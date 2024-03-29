"""Train a neural net."""

import argparse
import copy
import hashlib
import logging
import shutil
import time

import torch
from torch.utils.tensorboard import SummaryWriter

try:
    from timm.models.layers import DropPath
except ImportError:
    pass

# from ..logger_clearml import ClearML_Singleton

LOG = logging.getLogger(__name__)


class Trainer():
    epochs = None
    n_train_batches = None
    n_val_batches = None

    clip_grad_norm = 0.0
    clip_grad_value = 0.0
    log_interval = 11
    val_interval = 1

    fix_batch_norm = False
    stride_apply = 1
    ema_decay = 0.01
    train_profile = None
    distributed_reduce_loss = True

    def __init__(self, model, loss, optimizer, out, *,
                 checkpoint_shell=None,
                 lr_scheduler=None,
                 device=None,
                 model_meta_data=None):
        self.model = model
        self.loss = loss
        self.optimizer = optimizer
        self.out = out
        self.checkpoint_shell = checkpoint_shell
        self.lr_scheduler = lr_scheduler
        self.device = device
        self.model_meta_data = model_meta_data
        self.writer = SummaryWriter(log_dir=self.out)

        self.ema = None
        self.ema_restore_params = None

        self.n_clipped_grad = 0
        self.max_norm = 0.0

        if self.train_profile:
            # monkey patch to profile self.train_batch()
            self.trace_counter = 0
            self.train_batch_without_profile = self.train_batch

            def train_batch_with_profile(*args, **kwargs):
                with torch.autograd.profiler.profile(use_cuda=True) as prof:
                    result = self.train_batch_without_profile(*args, **kwargs)
                print(prof.key_averages())
                self.trace_counter += 1
                tracefilename = self.train_profile.replace(
                    '.json', '.{}.json'.format(self.trace_counter))
                LOG.info('writing trace file %s', tracefilename)
                prof.export_chrome_trace(tracefilename)
                return result

            self.train_batch = train_batch_with_profile
        self.condition_HG = False  # (isinstance(self.model, torch.nn.DataParallel) and self.model.module.base_net.__class__.__name__=="HGNet_base")\
        # or (not isinstance(self.model, torch.nn.DataParallel) and self.model.base_net.__class__.__name__=="HGNet_base")

        added_loss = []
        if self.condition_HG:
            nstack = self.model.module.base_net.nstack if isinstance(self.model,
                                                                     torch.nn.DataParallel) else self.model.base_net.nstack
            for i in range(nstack - 1):
                added_loss.append("hg_intermediate.stack{}".format(i))
        LOG.info({
            'type': 'config',
            'field_names': self.loss.field_names + added_loss,
        })

        self.clml_logger = None
        # if ClearML_Singleton.getInstance():
        # self.clml_logger = ClearML_Singleton.getInstance().get_logger()

    @classmethod
    def cli(cls, parser: argparse.ArgumentParser):
        group = parser.add_argument_group('trainer')
        group.add_argument('--epochs', type=int,
                           help='number of epochs to train')
        group.add_argument('--train-batches', default=None, type=int,
                           help='number of train batches')
        group.add_argument('--val-batches', default=None, type=int,
                           help='number of val batches')

        group.add_argument('--clip-grad-norm', default=cls.clip_grad_norm, type=float,
                           help='clip grad norm: specify largest change for single param')
        group.add_argument('--clip-grad-value', default=cls.clip_grad_value, type=float,
                           help='clip grad value: specify largest change for single param')
        group.add_argument('--log-interval', default=cls.log_interval, type=int,
                           help='log loss every n steps')
        group.add_argument('--val-interval', default=cls.val_interval, type=int,
                           help='validation run every n epochs')

        group.add_argument('--stride-apply', default=cls.stride_apply, type=int,
                           help='apply and reset gradients every n batches')
        assert not cls.fix_batch_norm
        group.add_argument('--fix-batch-norm',
                           default=False, const=True, type=int, nargs='?',
                           help='fix batch norm running statistics (optionally specify epoch)')
        group.add_argument('--ema', default=cls.ema_decay, type=float,
                           help='ema decay constant')
        group.add_argument('--profile', default=cls.train_profile,
                           help='enables profiling. specify path for chrome tracing file')

    @classmethod
    def configure(cls, args: argparse.Namespace):
        cls.epochs = args.epochs
        cls.n_train_batches = args.train_batches
        cls.n_val_batches = args.val_batches

        cls.clip_grad_norm = args.clip_grad_norm
        cls.clip_grad_value = args.clip_grad_value
        cls.log_interval = args.log_interval
        cls.val_interval = args.val_interval

        cls.fix_batch_norm = args.fix_batch_norm
        cls.stride_apply = args.stride_apply
        cls.ema_decay = args.ema
        cls.train_profile = args.profile

    def lr(self):
        for param_group in self.optimizer.param_groups:
            return param_group['lr']

    def step_ema(self):
        if self.ema is None:
            return

        for p, ema_p in zip(self.model.parameters(), self.ema):
            ema_p.mul_(1.0 - self.ema_decay).add_(p.data, alpha=self.ema_decay)

    def apply_ema(self):
        if self.ema is None:
            return

        LOG.info('applying ema')
        self.ema_restore_params = copy.deepcopy(
            [p.data for p in self.model.parameters()])
        for p, ema_p in zip(self.model.parameters(), self.ema):
            p.data.copy_(ema_p)

    def ema_restore(self):
        if self.ema_restore_params is None:
            return

        LOG.info('restoring params from before ema')
        for p, ema_p in zip(self.model.parameters(), self.ema_restore_params):
            p.data.copy_(ema_p)
        self.ema_restore_params = None

    def loop(self,
             train_scenes: torch.utils.data.DataLoader,
             val_scenes: torch.utils.data.DataLoader,
             start_epoch=0):
        if start_epoch >= self.epochs:
            raise Exception('start epoch ({}) >= total epochs ({})'
                            ''.format(start_epoch, self.epochs))

        if self.lr_scheduler is not None:
            assert self.lr_scheduler.last_epoch == start_epoch * len(train_scenes)

        for epoch in range(start_epoch, self.epochs):
            if epoch == 0:
                self.write_model(0, final=False)
            if hasattr(train_scenes.sampler, 'set_epoch'):
                train_scenes.sampler.set_epoch(epoch)
            if hasattr(val_scenes.sampler, 'set_epoch'):
                val_scenes.sampler.set_epoch(epoch)

            self.train(train_scenes, epoch)
            # self.val(val_scenes, epoch + 1)
            if (epoch + 1) % self.val_interval == 0 \
                    or  epoch + 1 == self.epochs:
                self.write_model(epoch + 1, epoch + 1 == self.epochs)
                self.val(val_scenes, epoch + 1)

    def transform_target(self, head):
        if head is None:
            return None
        elif isinstance(head, list) and len(head) == 2:
            return (head[0].to(self.device, non_blocking=True), head[1].to(self.device, non_blocking=True))
        elif isinstance(head, list):
            for i in range(len(head)):
                head[i] = head[i].to(self.device, non_blocking=True)
        else:
            head = head.to(self.device, non_blocking=True)
        return head

    def train_batch(self, data, targets, depth_information, apply_gradients=True):  # pylint: disable=method-hidden
        if self.device:
            # start_time=time.time()
            if isinstance(data, list):
                for data_idx in range(len(data)):
                    data[data_idx] = data[data_idx].to(self.device, non_blocking=True)
            else:
                data = data.to(self.device, non_blocking=True)
            if isinstance(depth_information, list):
                for data_idx in range(len(depth_information)):
                    depth_information[data_idx] = depth_information[data_idx].to(self.device, non_blocking=True)
            else:
                depth_information =depth_information.to(self.device, non_blocking=True)

            # targets = [head.to(self.device, non_blocking=True)
            #            if head is not None else None
            #            for head in targets]

            targets_temp = []
            for head in targets:
                if isinstance(head, dict):
                    for k in head.keys():
                        head[k] = self.transform_target(head[k])
                else:
                    head = self.transform_target(head)
                targets_temp.append(head)
            targets = targets_temp
            # end_time=time.time()
            # print(f"transfer data to gpu time is {end_time-start_time}")
            # print(f"depth type is {depth_information.dtype}")
            # print(f"type data is {data.dtype}")
        # train encoder

        with torch.autograd.profiler.record_function('model'):
            outputs,offset_loss = self.model(data, depth_information, targets, [t is not None for t in targets])
            if self.condition_HG:
                outputs, combined_hm_preds = outputs
        # forward_time=time.time()
        # print(f"forward time is {forward_time-end_time}")
        with torch.autograd.profiler.record_function('loss'):
            # depth_head,head=outputs
            # loss,head_losses = self.loss(depth_head, targets)
            # print('depth loss time is {}'.format(time.time()-forward_time))
            # depth_loss_time=time.time()
            loss, head_losses = self.loss(outputs, targets)
            # print(f"rgb time is {time.time()-forward_time}")
            # exit(0)
            if offset_loss is not None:
                # print(f"offset loss is {offset_loss}")
                loss=loss+torch.mean(offset_loss)
            if self.condition_HG:
                if isinstance(self.model, torch.nn.DataParallel):
                    loss_intermediate = self.model.module.base_net.calc_inter_loss(combined_hm_preds, torch.cat(
                        (targets[0][:, :, 0], targets[1][:, :, 1]), 1))
                else:
                    loss_intermediate = self.model.base_net.calc_inter_loss(combined_hm_preds, torch.cat(
                        (targets[0][:, :, 0], targets[1][:, :, 1]), 1))
                head_losses.extend(loss_intermediate)
                loss = loss + torch.stack(loss_intermediate, dim=0).sum()
        # print(f"loss time is {time.time()-forward_time}")
        # loss_time=time.time()
        if loss is not None:
            with torch.autograd.profiler.record_function('backward'):
                loss.backward()
        if self.clip_grad_norm:
            max_norm = self.clip_grad_norm / self.lr()
            total_norm = torch.nn.utils.clip_grad_norm_(
                self.model.parameters(), max_norm, norm_type=float('inf'))
            self.max_norm = max(float(total_norm), self.max_norm)
            if total_norm > max_norm:
                self.n_clipped_grad += 1
                print('CLIPPED GRAD NORM: total norm before clip: {}, max norm: {}'
                      ''.format(total_norm, max_norm))
        if self.clip_grad_value:
            torch.nn.utils.clip_grad_value_(self.model.parameters(), self.clip_grad_value)
        if apply_gradients:
            with torch.autograd.profiler.record_function('step'):
                self.optimizer.step()
                self.optimizer.zero_grad()
            with torch.autograd.profiler.record_function('ema'):
                self.step_ema()

        with torch.no_grad():
            loss = self.reduce_loss(loss)
            head_losses = self.reduce_loss(head_losses)
        # print(f"backward time is {time.time()-loss_time}")
        return (
            float(loss.item()) if loss is not None else None,
            [float(l.item()) if l is not None else None
             for l in head_losses],
        )

    @classmethod
    def reduce_loss(cls, loss):
        if not cls.distributed_reduce_loss:
            return loss
        if loss is None:
            return loss
        if not torch.distributed.is_initialized():
            return loss

        if isinstance(loss, (list, tuple)):
            return [cls.reduce_loss(l) for l in loss]

        # average loss from all processes
        torch.distributed.reduce(loss, 0)
        if torch.distributed.get_rank() == 0:
            loss = loss / torch.distributed.get_world_size()
        return loss

    def val_batch(self, data, targets, depth_information):
        if self.device:
            if isinstance(data, list):
                for data_idx in range(len(data)):
                    data[data_idx] = data[data_idx].to(self.device, non_blocking=True)
            else:
                data = data.to(self.device, non_blocking=True)
            # targets = [head.to(self.device, non_blocking=True)
            #            if head is not None else None
            #            for head in targets]
            if isinstance(depth_information, list):
                for data_idx in range(len(depth_information)):
                    depth_information[data_idx] = depth_information[data_idx].to(self.device, non_blocking=True)
            else:
                depth_information = depth_information.to(self.device, non_blocking=True)
            targets_temp = []
            for head in targets:
                if isinstance(head, dict):
                    for k in head.keys():
                        head[k] = self.transform_target(head[k])
                else:
                    head = self.transform_target(head)
                targets_temp.append(head)
            targets = targets_temp

        with torch.no_grad():
            outputs,offset_loss = self.model(data, depth_information)
            if self.condition_HG:
                outputs, combined_hm_preds = outputs
            loss, head_losses = self.loss(outputs, targets)
            if offset_loss is not None:
                loss=loss+torch.mean(offset_loss)
            if self.condition_HG:
                if isinstance(self.model, torch.nn.DataParallel):
                    loss_intermediate = self.model.module.base_net.calc_inter_loss(combined_hm_preds, torch.cat(
                        (targets[0][:, :, 0], targets[1][:, :, 1]), 1))
                else:
                    loss_intermediate = self.model.base_net.calc_inter_loss(combined_hm_preds, torch.cat(
                        (targets[0][:, :, 0], targets[1][:, :, 1]), 1))
                head_losses.extend(loss_intermediate)
                loss = loss + torch.stack(loss_intermediate, dim=0).sum()

            loss = self.reduce_loss(loss)
            head_losses = self.reduce_loss(head_losses)

        return (
            float(loss.item()) if loss is not None else None,
            [float(l.item()) if l is not None else None
             for l in head_losses],
        )
    def fix_depth_head(self,model):
        nets= [model.depth_head_nets[0],model.depth_head_nets[1]]
        for net in nets:
            #fix all the batchnorms in the depth head
            for m in net.modules():
                if isinstance(m, (torch.nn.BatchNorm1d, torch.nn.BatchNorm2d)):
                    LOG.debug('eval mode for: %s', m)
                    m.eval()
    # pylint: disable=too-many-branches
    def train(self, scenes, epoch):
        start_time = time.time()
        self.model.train()
        if type(self.model)==torch.nn.DataParallel:
            if self.model.module.mode=='combine':
                self.fix_depth_head(self.model.module)
        else:
            if self.model.mode=='combine':
                self.fix_depth_head(self.model)
        if self.fix_batch_norm is True \
                or (self.fix_batch_norm is not False and self.fix_batch_norm <= epoch):
            LOG.info('fix batchnorm')
            for m in self.model.modules():
                if isinstance(m, (torch.nn.BatchNorm1d, torch.nn.BatchNorm2d)):
                    LOG.debug('eval mode for: %s', m)
                    m.eval()

        self.ema_restore()
        self.ema = None

        epoch_loss = 0.0
        head_epoch_losses = None
        head_epoch_counts = None
        last_batch_end = time.time()
        self.optimizer.zero_grad()
        for batch_idx, (data, target, _, depth_information) in enumerate(scenes):
            preprocess_time = time.time() - last_batch_end
            batch_start = time.time()
            apply_gradients = batch_idx % self.stride_apply == 0
            loss, head_losses = self.train_batch(data, target, depth_information, apply_gradients)

            # update epoch accumulates
            if loss is not None:
                epoch_loss += loss
            if head_epoch_losses is None:
                head_epoch_losses = [0.0 for _ in head_losses]
                head_epoch_counts = [0 for _ in head_losses]
            for i, head_loss in enumerate(head_losses):
                if head_loss is None:
                    continue
                head_epoch_losses[i] += head_loss
                head_epoch_counts[i] += 1

            batch_time = time.time() - batch_start

            # write training loss
            if batch_idx % self.log_interval == 0:
                batch_info = {
                    'type': 'train',
                    'epoch': epoch, 'batch': batch_idx, 'n_batches': len(scenes),
                    'time': round(batch_time, 3),
                    'data_time': round(preprocess_time, 3),
                    'lr': round(self.lr(), 8),
                    'loss': round(loss, 3) if loss is not None else None,
                    'head_losses': [round(l, 3) if l is not None else None
                                    for l in head_losses],
                }
                self.writer.add_scalar('train/total_loss', loss, epoch * len(scenes) + batch_idx)
                head_key = ["vg_cn.centernet.c", "vg_cn.centernet.vec1", "vg_cn.centernet.vec2", "vg_cn.raf.c",
                            "vg_cn.raf.vec1", "vg_cn.raf.vec2", "vg_cn.raf.scales1", "vg_cn.raf.scales2",'depth_head']
                for key_name, head_loss in zip(head_key, head_losses):
                    self.writer.add_scalar(f"train/{key_name}", head_loss, epoch * len(scenes) + batch_idx)
                if hasattr(self.loss, 'batch_meta'):
                    batch_info.update(self.loss.batch_meta())
                LOG.info(batch_info)

            # initialize ema
            if self.ema is None and self.ema_decay:
                self.ema = copy.deepcopy([p.data for p in self.model.parameters()])

            # update learning rate
            if self.lr_scheduler is not None:
                self.lr_scheduler.step()

            if self.n_train_batches and batch_idx + 1 >= self.n_train_batches:
                break

            last_batch_end = time.time()

        self.apply_ema()
        LOG.info({
            'type': 'train-epoch',
            'epoch': epoch + 1,
            'loss': round(epoch_loss / len(scenes), 5),
            'head_losses': [round(l / max(1, c), 5)
                            for l, c in zip(head_epoch_losses, head_epoch_counts)],
            'time': round(time.time() - start_time, 1),
            'n_clipped_grad': self.n_clipped_grad,
            'max_norm': self.max_norm,
        })
        self.n_clipped_grad = 0
        self.max_norm = 0.0

        # if self.clml_logger:
        # self.clml_logger.report_scalar(title='Epoch Loss',
        #        series='Train Loss', value=round(epoch_loss / len(scenes), 5), iteration=epoch + 1)

    def val(self, scenes, epoch):
        start_time = time.time()

        # Train mode implies outputs are for losses, so have to use it here.
        self.model.train()
        if self.fix_batch_norm is True \
                or (self.fix_batch_norm is not False and self.fix_batch_norm <= epoch - 1):
            LOG.info('fix batchnorm')
            for m in self.model.modules():
                if isinstance(m, (torch.nn.BatchNorm1d, torch.nn.BatchNorm2d, torch.nn.SyncBatchNorm)):
                    LOG.debug('eval mode for: %s', m)
                    m.eval()

        for m in self.model.modules():
            if isinstance(m, (torch.nn.BatchNorm1d, torch.nn.BatchNorm2d, torch.nn.SyncBatchNorm)):
                LOG.debug('eval mode for: %s', m)
                m.eval()
            if isinstance(m, DropPath):
                LOG.debug('eval mode for: %s', m)
                m.eval()

        epoch_loss = 0.0
        head_epoch_losses = None
        head_epoch_counts = None
        for batch_idx, (data, target, _, depth_info) in enumerate(scenes):
            loss, head_losses = self.val_batch(data, target, depth_info)

            # update epoch accumulates
            if loss is not None:
                epoch_loss += loss
            if head_epoch_losses is None:
                head_epoch_losses = [0.0 for _ in head_losses]
                head_epoch_counts = [0 for _ in head_losses]
            for i, head_loss in enumerate(head_losses):
                if head_loss is None:
                    continue
                head_epoch_losses[i] += head_loss
                head_epoch_counts[i] += 1

            if self.n_val_batches and batch_idx + 1 >= self.n_val_batches:
                break

        eval_time = time.time() - start_time

        LOG.info({
            'type': 'val-epoch',
            'epoch': epoch,
            'loss': round(epoch_loss / len(scenes), 5),
            'head_losses': [round(l / max(1, c), 5)
                            for l, c in zip(head_epoch_losses, head_epoch_counts)],
            'time': round(eval_time, 1),
        })
        self.writer.add_scalar('val/loss',loss,epoch)
        head_key = ["vg_cn.centernet.c", "vg_cn.centernet.vec1", "vg_cn.centernet.vec2", "vg_cn.raf.c",
                    "vg_cn.raf.vec1", "vg_cn.raf.vec2", "vg_cn.raf.scales1", "vg_cn.raf.scales2",'depth_head']
        for key_name, head_loss in zip(head_key, head_losses):
            self.writer.add_scalar(f"val/{key_name}", head_loss, epoch )
        # if self.clml_logger:
        # self.clml_logger.report_scalar(title='Epoch Loss',
        #    series='Val Loss', value=round(epoch_loss / len(scenes), 5), iteration=epoch)

    def write_model(self, epoch, final=True):
        if torch.distributed.is_initialized() and torch.distributed.get_rank() != 0:
            return

        model_to_save = self.model
        if self.checkpoint_shell is not None:
            model = self.model if not hasattr(self.model, 'module') else self.model.module
            self.checkpoint_shell.load_state_dict(model.state_dict())
            model_to_save = self.checkpoint_shell

        filename = '{}.epoch{:03d}'.format(self.out, epoch)
        LOG.debug('about to write model')
        torch.save({
            'model': model_to_save,
            'epoch': epoch,
            'meta': self.model_meta_data,
        }, filename)
        LOG.info('model written: %s', filename)

        if final:
            sha256_hash = hashlib.sha256()
            with open(filename, 'rb') as f:
                for byte_block in iter(lambda: f.read(8192), b''):
                    sha256_hash.update(byte_block)
            file_hash = sha256_hash.hexdigest()
            outname, _, outext = self.out.rpartition('.')
            final_filename = '{}-{}.{}'.format(outname, file_hash[:8], outext)
            shutil.copyfile(filename, final_filename)
