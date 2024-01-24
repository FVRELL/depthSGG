import argparse
import logging
import math

import torch
from torch import nn

import openpifpaf
from openpifpaf import headmeta
from openpifpaf.network.heads import index_field_torch
from torch.nn.modules.utils import _pair
from torchvision.ops import DeformConv2d
LOG = logging.getLogger(__name__)

class convolution(nn.Module):
    def __init__(self, k, inp_dim, out_dim, stride=1, with_bn=True):
        super(convolution, self).__init__()

        pad = (k - 1) // 2
        self.conv = nn.Conv2d(inp_dim, out_dim, (k, k), padding=(pad, pad), stride=(stride, stride), bias=not with_bn)
        self.bn = None
        if with_bn:
            self.bn   = nn.BatchNorm2d(out_dim) if with_bn else nn.Sequential()
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        out = self.conv(x)
        if self.bn:
            out   = self.bn(out)
        relu = self.relu(out)
        return relu

class RefinedCompositeField3(openpifpaf.network.HeadNetwork):
    dropout_p = 0.0
    inplace_ops = True

    def __init__(self,
                 meta: headmeta.Base,
                 in_features, *,
                 kernel_size=1, padding=0, dilation=1):
        super().__init__(meta, in_features)

        LOG.debug('%s config: fields = %d, confidences = %d, vectors = %d, scales = %d '
                  'kernel = %d, padding = %d, dilation = %d',
                  meta.name, meta.n_fields, meta.n_confidences, meta.n_vectors, meta.n_scales,
                  kernel_size, padding, dilation)

        self.dropout = torch.nn.Dropout2d(p=self.dropout_p)

        # convolution
        self.conv_cnf = None
        self.conv_regr = None
        self.conv_scales = None

        self.conv_offset_subj = nn.Conv2d(
            in_features,
            2 * 1 * 1 * 1,
            kernel_size=1,
            stride=_pair(1),
            padding=_pair(0),
            bias=True)

        self.conv_offset_obj = nn.Conv2d(
            in_features,
            2 * 1 * 1 * 1,
            kernel_size=1,
            stride=_pair(1),
            padding=_pair(0),
            bias=True)

        self.deform_subj = DeformConv2d(in_features, in_features, kernel_size=1, bias=False)
        self.deform_obj = DeformConv2d(in_features, in_features, kernel_size=1, bias=False)
        self.init_offset()

        in_features = 3*in_features
        if meta.n_confidences > 0 :
            self.conv_cnf = nn.Sequential(
                                convolution(1, in_features, in_features, with_bn=False),
                                nn.Conv2d(in_features, meta.n_fields * (meta.n_confidences)* (meta.upsample_stride ** 2), (1, 1))
                            )
        if meta.n_vectors > 0:
            self.conv_regr = nn.Sequential(
                                convolution(1, in_features, in_features, with_bn=False),
                                nn.Conv2d(in_features, meta.n_fields * (2 * 3)* (meta.upsample_stride ** 2), (1, 1))
                            )
        if meta.n_scales > 0:
            self.conv_scales = nn.Sequential(
                                convolution(1, in_features, in_features, with_bn=False),
                                nn.Conv2d(in_features, meta.n_fields * (meta.n_scales)* (meta.upsample_stride ** 2), (1, 1))
                            )

        # upsample
        assert meta.upsample_stride >= 1
        self.upsample_op = None
        if meta.upsample_stride > 1:
            self.upsample_op = torch.nn.PixelShuffle(meta.upsample_stride)

    def init_offset(self):
        self.conv_offset_subj.weight.data.zero_()
        self.conv_offset_subj.bias.data.zero_()
        self.conv_offset_obj.weight.data.zero_()
        self.conv_offset_obj.bias.data.zero_()

        # self.deform_subj.weight.data.fill_(1)
        # self.deform_obj.weight.data.fill_(1)
        #self.deform_subj.bias.data.zero_()
        #self.deform_obj.bias.data.zero_()

    @classmethod
    def cli(cls, parser: argparse.ArgumentParser):
        group = parser.add_argument_group('RefiCompositeField3')
        group.add_argument('--rcf3-dropout', default=cls.dropout_p, type=float,
                           help='[experimental] zeroing probability of feature in head input')
        assert cls.inplace_ops
        group.add_argument('--rcf3-no-inplace-ops', dest='cf3_inplace_ops',
                           default=True, action='store_false',
                           help='alternative graph without inplace ops')

    @classmethod
    def configure(cls, args: argparse.Namespace):
        cls.dropout_p = args.cf3_dropout
        cls.inplace_ops = args.cf3_inplace_ops

    @property
    def sparse_task_parameters(self):
        return [self.conv.weight]

    def forward(self, x_input):  # pylint: disable=arguments-differ
        if isinstance(x_input, tuple):
            feature_input = x_input[0]
            targets_offsets = x_input[1][1][1]
        else:
            feature_input = x_input

        x = self.dropout(feature_input)

        subj_offset = self.conv_offset_subj(x)
        obj_offset = self.conv_offset_obj(x)

        if self.meta.n_offsets >0:
            offsets_x = torch.cat([subj_offset, obj_offset], dim=1)

        if self.training and isinstance(x_input, tuple):
            subj_offset = torch.clone(targets_offsets[:,:2]).detach()
            obj_offset = torch.clone(targets_offsets[:,2:]).detach()
            subj_offset[torch.isnan(subj_offset)] = 0
            obj_offset[torch.isnan(obj_offset)] = 0

        x = torch.cat([x, self.deform_subj(x, subj_offset), self.deform_obj(x,obj_offset)], dim=1)
        tensor_toconcat = []
        classes_x = None
        if self.conv_cnf is not None:
            classes_x = self.conv_cnf(x)
            tensor_toconcat.append(classes_x)

        regr_x = None
        if self.conv_regr is not None:
            regr_x = self.conv_regr(x)
            tensor_toconcat.append(regr_x)



        scales_x = None
        if self.conv_scales is not None:
            scales_x = self.conv_scales(x)
            tensor_toconcat.append(scales_x)

        x = torch.cat(tensor_toconcat, dim=1)
        # upscale
        if self.upsample_op is not None:
            x = self.upsample_op(x)
            low_cut = (self.meta.upsample_stride - 1) // 2
            high_cut = math.ceil((self.meta.upsample_stride - 1) / 2.0)
            if self.training:
                # negative axes not supported by ONNX TensorRT
                x = x[:, :, low_cut:-high_cut, low_cut:-high_cut]
            else:
                # the int() forces the tracer to use static shape
                x = x[:, :, low_cut:int(x.shape[2]) - high_cut, low_cut:int(x.shape[3]) - high_cut]

        # Extract some shape parameters once.
        # Convert to int so that shape is constant in ONNX export.
        x_size = x.size()
        batch_size = int(x_size[0])
        feature_height = int(x_size[2])
        feature_width = int(x_size[3])
        x = x.view(
            batch_size,
            self.meta.n_fields,
            self.meta.n_confidences + self.meta.n_vectors * 3 + self.meta.n_scales,
            feature_height,
            feature_width
        )

        if not self.training and self.inplace_ops:
            # classification
            classes_x = x[:, :, 0:self.meta.n_confidences]
            torch.sigmoid_(classes_x)

            # regressions x: add index
            if self.meta.n_vectors > 0:
                index_field = index_field_torch(x.shape[-2:], device=x.device)
                first_reg_feature = self.meta.n_confidences
                for i, do_offset in enumerate(self.meta.vector_offsets):
                    if not do_offset:
                        continue
                    reg_x = x[:, :, first_reg_feature + i * 2:first_reg_feature + (i + 1) * 2]
                    reg_x.add_(index_field)

            # scale
            first_scale_feature = self.meta.n_confidences + self.meta.n_vectors * 3 +self.meta.n_offsets*2
            scales_x = x[:, :, first_scale_feature:first_scale_feature + self.meta.n_scales]
            #torch.exp_(scales_x)
            scales_x[:] = torch.nn.functional.softplus(scales_x)
        elif not self.training and not self.inplace_ops:
            # TODO: CoreMLv4 does not like strided slices.
            # Strides are avoided when switching the first and second dim
            # temporarily.
            x = torch.transpose(x, 1, 2)

            # classification
            classes_x = x[:, 0:self.meta.n_confidences]
            classes_x = torch.sigmoid(classes_x)

            # regressions x
            first_reg_feature = self.meta.n_confidences
            regs_x = [
                x[:, first_reg_feature + i * 2:first_reg_feature + (i + 1) * 2]
                for i in range(self.meta.n_vectors)
            ]
            # regressions x: add index
            index_field = index_field_torch(x.shape[-2:], device=x.device, unsqueeze=(1, 0))
            regs_x = [torch.add(reg_x, index_field) if do_offset else reg_x
                      for reg_x, do_offset in zip(regs_x, self.meta.vector_offsets)]

            # regressions logb
            first_reglogb_feature = self.meta.n_confidences + self.meta.n_vectors * 2
            regs_logb = x[:, first_reglogb_feature:first_reglogb_feature + self.meta.n_vectors]

            # scale
            first_scale_feature = self.meta.n_confidences + self.meta.n_vectors * 3 +self.meta.n_offsets*2
            scales_x = x[:, first_scale_feature:first_scale_feature + self.meta.n_scales]
            #scales_x = torch.exp(scales_x)
            scales_x = torch.nn.functional.softplus(scales_x)

            # concat
            x = torch.cat([classes_x, *regs_x, regs_logb, scales_x], dim=1)

            # TODO: CoreMLv4 problem (see above).
            x = torch.transpose(x, 1, 2)

        if self.training and self.meta.n_offsets >0:
            return x, offsets_x

        return x
