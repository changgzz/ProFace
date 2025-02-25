import os
from PPFR_model import ClientBackbone


from collections import OrderedDict
import torch

import torch.optim as optim
import torch.cuda.amp as amp
import torch.distributed as dist
import torch.nn.init as init
from torch.utils.data import DataLoader
from torchkit.head import get_head
from torchkit.hooks import CheckpointHook, LogHook, SummaryHook, LearningRateHook
from torchkit.util import load_config, get_class_split
from torchkit.util import separate_resnet_bn_paras
from torchkit.util import CkptLoader, CkptSaver
from torchkit.data import MultiDataset, MultiDistributedSampler
import torchvision.transforms as transforms
from torchkit.util.utils import init_log


class BranchMetaInfo(object):
    """ BranchMetaInfo class
    """

    def __init__(self, name, batch_size, weight=1.0, scale=64.0, margin=0.5):
        self.name = name
        self.batch_size = batch_size
        self.weight = weight
        self.scale = scale
        self.margin = margin


class BaseTask(object):
    def __init__(self, cfg_file):
        """ Create a ``BaseTask`` object
            A ``BaseTask`` object will create a base class about training scheduler

            Args:
            cfg_file: config file, which defines datasets, backbone and head names, loss name
            and some training envs
        """
        self.cfg = load_config(cfg_file)
        self.rank = self.cfg['RANK']
        self.local_rank = self.cfg['LOCAL_RANK']
        self.world_size = self.cfg['WORLD_SIZE']

        self.step_per_epoch = 0
        self.warmup_step = self.cfg[
            'WARMUP_STEP']
        self.start_epoch = self.cfg['START_EPOCH']
        self.epoch_num = self.cfg['NUM_EPOCH']

        self.input_size = self.cfg['INPUT_SIZE']
        self.branches = OrderedDict()
        self.train_loader = None
        self.blur_loader = None

        self.val_data_root = self.cfg['VAL_DATA_ROOT']

        self.dist_fc = self.cfg.get('DIST_FC', True)
        self.amp = self.cfg.get('AMP', False)

        self.backbone = None
        self.heads = OrderedDict()
        self.summary = OrderedDict()
        self.log_buffer = OrderedDict()
        self.scaler = amp.GradScaler() if self.amp else None

        self.logging = init_log(self.cfg['LOGS'])
        # parsing DATASETS in config yaml
        for branch in self.cfg['DATASETS']:
            branch_meta = BranchMetaInfo(branch['name'], branch['batch_size'])
            if 'weight' in branch:
                branch_meta.weight = branch['weight']
            if 'scale' in branch:
                branch_meta.scale = branch['scale']
            if 'margin' in branch:
                branch_meta.margin = branch['margin']
            self.branches[branch_meta.name] = branch_meta
            str = "Dataset %s, batch_size %d, weight %f, scale %d, margin %f" % (
                branch_meta.name,
                branch_meta.batch_size,
                branch_meta.weight,
                branch_meta.scale,
                branch_meta.margin
            )
            self.logging.info(str)

        self.batch_sizes = [branch.batch_size for branch in self.branches.values()]
        self.branch_weights = [branch.weight for branch in self.branches.values()]

    def init_env(self):
        """ Init distribution env
        """
        os.environ['MASTER_ADDR'] = 'localhost'
        os.environ['MASTER_PORT'] = '5166'

        seed = self.cfg['SEED']
        torch.manual_seed(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = True

        dist.init_process_group(backend=self.cfg['DIST_BACKEND'], init_method=self.cfg["DIST_URL"],
                                rank=self.cfg['RANK'], world_size=self.cfg['WORLD_SIZE'])

        self.world_size = dist.get_world_size()
        self.rank = dist.get_rank()
        self.local_rank = int(os.environ.get('LOCAL_RANK', 0))
        self.logging.info("world_size: %s, rank: %d, local_rank: %d" %
                          (self.world_size, self.rank, self.local_rank))

        self.cfg['WORLD_SIZE'] = self.world_size
        self.cfg['RANK'] = self.rank

    def register_hooks(self):
        """ register hooks
        LogHook
            |
        SummaryHook
            |
        CheckpointHook
            |
        LearningRateHook
        """
        self._hooks = []

        log_hook = LogHook(100, self.rank)
        self._hooks.append(log_hook)

        # SummaryHook, write tensorboard summery in training
        summary_hook = SummaryHook(self.cfg['LOG_ROOT'], 100, self.rank)
        self._hooks.append(summary_hook)

        save_epochs = self.cfg.get('SAVE_EPOCHS', None)
        if save_epochs is None:
            save_epochs = [x + 1 for x in range(self.cfg['NUM_EPOCH'])]
        checkpoint_hook = CheckpointHook(save_epochs)
        self._hooks.append(checkpoint_hook)

        learning_rate_hook = LearningRateHook(self.cfg['LRS'], self.cfg['STAGES'], self.cfg['WARMUP_STEP'])
        self._hooks.append(learning_rate_hook)

    def call_hook(self, fn_name, *args):
        for hook in self._hooks:
            getattr(hook, fn_name)(self, *args)

    def make_inputs(self):
        """ make datasets
        """
        rgb_mean = self.cfg['RGB_MEAN']
        rgb_std = self.cfg['RGB_STD']
        transform = transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize((112, 112)),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(mean=rgb_mean, std=rgb_std)
        ])

        # --------------------------CelebA_dataloader------------------------------
        ds_names = list(self.branches.keys())
        ds = MultiDataset(self.cfg['DATA_ROOT'], self.cfg['INDEX_ROOT'], ds_names, transform)
        ds.make_dataset(shard=True)
        self.class_nums = ds.class_nums
        sampler = MultiDistributedSampler(ds, self.batch_sizes)
        self.train_loader = DataLoader(ds, sum(self.batch_sizes), shuffle=False, num_workers=self.cfg["NUM_WORKERS"],
                                       pin_memory=True, sampler=sampler)

        self.step_per_epoch = len(self.train_loader)
        self.logging.info("Step_per_epoch = %d" % self.step_per_epoch)

    def make_model(self):
        """ build training backbone and heads
        """
        backbone_name = self.cfg['BACKBONE_NAME']

        self.backbone = ClientBackbone(112, self.cfg["EMBEDDING_SIZE"])
        self.backbone = self.backbone.cuda()

        self.logging.info("{} Backbone Generated".format(backbone_name))

        embedding_size = self.cfg['EMBEDDING_SIZE']
        self.class_shards = []
        metric = get_head(self.cfg['HEAD_NAME'], dist_fc=self.dist_fc)

        for name, branch in self.branches.items():
            class_num = self.class_nums[name]
            class_shard = get_class_split(class_num, self.world_size)
            self.class_shards.append(class_shard)
            self.logging.info('Split FC: {}'.format(class_shard))

            init_value = torch.FloatTensor(embedding_size, class_num)
            init.normal_(init_value, std=0.01)
            # ArcFace
            head = metric(in_features=embedding_size,
                          gpu_index=self.rank,
                          weight_init=init_value,
                          class_split=class_shard,
                          scale=branch.scale,
                          margin=branch.margin)
            del init_value
            head = head.cuda()
            self.heads[name] = head

    def get_optimizer(self):
        """ build optimizers
        """
        backbone_paras_only_bn, backbone_paras_wo_bn = separate_resnet_bn_paras(self.backbone)

        learning_rates = self.cfg['LRS']
        init_lr = learning_rates[0]
        weight_decay = self.cfg['WEIGHT_DECAY']
        momentum = self.cfg['MOMENTUM']
        backbone_opt = optim.SGD([
            {'params': backbone_paras_wo_bn, 'weight_decay': weight_decay},
            {'params': backbone_paras_only_bn},
        ], lr=init_lr, momentum=momentum)

        # ArcFace
        head_opts = OrderedDict()
        for name, head in self.heads.items():
            opt = optim.SGD([{'params': head.parameters()}], lr=init_lr, momentum=momentum, weight_decay=4e-4)
            head_opts[name] = opt

        optimizer = {
            'backbone': backbone_opt,
            'heads': head_opts,
        }
        return optimizer

    def update_log_buffer(self, vars):
        for key, val in vars.items():
            self.log_buffer[key] = val

    def update_summary(self, vars):
        for key, val in vars.items():
            self.summary[key] = val

    def save_ckpt(self, epoch):
        """ save ckpt
        """
        self.model_root = self.cfg['MODEL_ROOT']

        CkptSaver.save_backbone(self.backbone, self.model_root, epoch, self.rank)
        CkptSaver.save_heads(self.heads, self.model_root, epoch, self.dist_fc, self.rank)

        if isinstance(self.opt, dict):
            backbone_opt = self.opt['backbone']
            meta_dict = {
                'EPOCH': epoch,
                'BACKBONE_OPT': backbone_opt.state_dict(),
            }
        else:
            meta_dict = {
                'EPOCH': epoch,
                'OPTIMIZER': self.opt.state_dict(),
            }
        if self.amp:
            meta_dict["AMP_SCALER"] = self.scaler.state_dict()

        self.logging.info("Save checkpoint at epoch %d ..." % epoch)

    def load_pretrain_model(self):
        """ load pretrain model ckpt if training mode is finetuning
        """
        backbone_resume = self.cfg.get('BACKBONE_RESUME', '')
        if backbone_resume != '':
            CkptLoader.load_backbone(self.backbone, backbone_resume, self.local_rank)

        head_resume = self.cfg.get('HEAD_RESUME', '')
        if head_resume != '':
            CkptLoader.load_head(self.heads, head_resume, self.dist_fc, self.rank)

    def loop_step(self, epoch):
        """ Implemented by sub class, which run in every training step
        """
        raise NotImplementedError()

    def train(self):
        raise NotImplementedError()
