#!/usr/bin/env python3
""" ImageNet Training Script for MindSpore """

import argparse
import time
import yaml
import os
import logging
from collections import OrderedDict
from datetime import datetime

import mindspore as ms
import mindspore.nn as nn
from mindspore import context, Tensor
from mindspore.communication import init, get_rank, get_group_size
from mindspore.train import Model
from mindspore.train.callback import LossMonitor, TimeMonitor, CheckpointConfig, ModelCheckpoint
from mindspore.dataset import vision, transforms
from mindspore.dataset import ImageFolderDataset, MnistDataset
import mindspore as ms
import mindspore.nn as nn
import mindspore.ops as ops
from mindspore import Tensor, Parameter
from mindspore.common.initializer import initializer, TruncatedNormal, Constant


class MLP(nn.Cell):
    def __init__(self, dim, mlp_ratio=4):
        super(MLP, self).__init__()
        self.norm = LayerNorm(dim, eps=1e-6, data_format="channels_first")
        self.fc1 = nn.Conv2d(dim, dim * mlp_ratio, kernel_size=1, has_bias=True)
        self.pos = nn.Conv2d(dim * mlp_ratio, dim * mlp_ratio, kernel_size=3, pad_mode='pad', padding=1, group=dim * mlp_ratio, has_bias=True)
        self.fc2 = nn.Conv2d(dim * mlp_ratio, dim, kernel_size=1, has_bias=True)
        self.act = nn.GELU()

    def construct(self, x):
        x = self.norm(x)
        x = self.fc1(x)
        x = self.act(x)
        x = x + self.act(self.pos(x))
        x = self.fc2(x)
        return x

class SpatialAttention(nn.Cell):
    def __init__(self, dim, kernel_size, expand_ratio=2):
        super(SpatialAttention, self).__init__()
        self.norm = LayerNorm(dim, eps=1e-6, data_format="channels_first")
        self.att = nn.SequentialCell(
            nn.Conv2d(dim, dim, kernel_size=1, has_bias=True),
            nn.GELU(),
            nn.Conv2d(dim, dim, kernel_size=kernel_size, pad_mode='pad', padding=kernel_size//2, group=dim, has_bias=True)
        )
        self.v = nn.Conv2d(dim, dim, kernel_size=1, has_bias=True)
        self.proj = nn.Conv2d(dim, dim, kernel_size=1, has_bias=True)

    def construct(self, x):
        x = self.norm(x)        
        x = self.att(x) * self.v(x)
        x = self.proj(x)
        return x

class DropPath(nn.Cell):
    """Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks)."""
    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.keep_prob = 1.0 - drop_prob
        self.rand = ops.UniformReal()
        self.floor = ops.Floor()
        self.div = ops.Div()

    def construct(self, x):
        if self.training and self.drop_prob > 0.0:
            random_tensor = self.rand((x.shape[0],) + (1,) * (x.ndim - 1)) 
            random_tensor += self.keep_prob
            random_tensor = self.floor(random_tensor)
            x = self.div(x, self.keep_prob) * random_tensor
        return x

class Block(nn.Cell):
    def __init__(self, index, dim, kernel_size, num_head, window_size=14, mlp_ratio=4., drop_path=0.):
        super(Block, self).__init__()
        self.attn = SpatialAttention(dim, kernel_size)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.mlp = MLP(dim, mlp_ratio)
        layer_scale_init_value = 1e-6
        
        self.layer_scale_1 = Parameter(
            Tensor(layer_scale_init_value * ops.ones((dim), ms.float32)), requires_grad=True)
        self.layer_scale_2 = Parameter(
            Tensor(layer_scale_init_value * ops.ones((dim), ms.float32)), requires_grad=True)

    def construct(self, x):
        x = x + self.drop_path(self.layer_scale_1.reshape(1, -1, 1, 1) * self.attn(x))
        x = x + self.drop_path(self.layer_scale_2.reshape(1, -1, 1, 1) * self.mlp(x))
        return x

class Conv2Former(nn.Cell):
    def __init__(self, kernel_size, img_size=224, in_chans=3, num_classes=1000, 
                 depths=[3, 3, 9, 3], dims=[96, 192, 384, 768], window_sizes=[14, 14, 14, 7],
                 mlp_ratios=[4, 4, 4, 4], num_heads=[2, 4, 10, 16], layer_scale_init_value=1e-6, 
                 head_init_scale=1., drop_path_rate=0., drop_rate=0.):
        super(Conv2Former, self).__init__()
        self.num_classes = num_classes
        self.depths = depths

        self.downsample_layers = nn.CellList()
        stem = nn.SequentialCell(
            nn.Conv2d(in_chans, dims[0] // 2, kernel_size=3, stride=2, pad_mode='pad', padding=1, has_bias=False),
            nn.GELU(),
            nn.BatchNorm2d(dims[0] // 2),
            nn.Conv2d(dims[0] // 2, dims[0] // 2, kernel_size=3, stride=1, pad_mode='pad', padding=1, has_bias=False),
            nn.GELU(),
            nn.BatchNorm2d(dims[0] // 2),
            nn.Conv2d(dims[0] // 2, dims[0], kernel_size=2, stride=2, has_bias=False),
        )
        self.downsample_layers.append(stem)
        
        for i in range(len(dims)-1):
            stride = 2
            downsample_layer = nn.SequentialCell(
                LayerNorm(dims[i], eps=1e-6, data_format="channels_first"),
                nn.Conv2d(dims[i], dims[i+1], kernel_size=stride, stride=stride, has_bias=True)
            )
            self.downsample_layers.append(downsample_layer)

        self.stages = nn.CellList()
        dp_rates = [x for x in ops.linspace(Tensor(0, ms.float32), Tensor(drop_path_rate, ms.float32), sum(depths))]
        cur = 0
        for i in range(len(dims)):
            stage = nn.SequentialCell([
                Block(cur+j, dims[i], kernel_size, num_heads[i], window_sizes[i], 
                mlp_ratios[i], dp_rates[cur+j]) for j in range(depths[i])
            ])
            self.stages.append(stage)
            cur += depths[i]

        self.head = nn.SequentialCell(
            nn.Conv2d(dims[-1], 1280, kernel_size=1, has_bias=True),
            nn.GELU(),
            LayerNorm(1280, eps=1e-6, data_format="channels_first")
        )
        self.pred = nn.Dense(1280, num_classes)
        self._init_weights()

    def _init_weights(self):
        for _, cell in self.cells_and_names():
            if isinstance(cell, (nn.Conv2d, nn.Dense)):
                cell.weight.set_data(initializer(TruncatedNormal(sigma=0.02), cell.weight.shape, cell.weight.dtype))
                if cell.bias is not None:
                    cell.bias.set_data(initializer('zeros', cell.bias.shape, cell.bias.dtype))
            elif isinstance(cell, LayerNorm):
                cell.bias.set_data(initializer('zeros', cell.bias.shape, cell.bias.dtype))
                cell.weight.set_data(initializer('ones', cell.weight.shape, cell.weight.dtype))

    def construct_features(self, x):
        for i in range(4):
            x = self.downsample_layers[i](x)
            x = self.stages[i](x)
        x = self.head(x)
        return x.mean(axis=(-2, -1))  # Global average pooling

    def construct(self, x):
        x = self.construct_features(x)
        x = self.pred(x)
        return x

class LayerNorm(nn.Cell):
    def __init__(self, normalized_shape, eps=1e-6, data_format="channels_last"):
        super(LayerNorm, self).__init__()
        self.weight = Parameter(initializer('ones', normalized_shape), name='weight')
        self.bias = Parameter(initializer('zeros', normalized_shape), name='bias')
        self.eps = eps
        self.data_format = data_format
        self.reduce_mean = ops.ReduceMean(keep_dims=True)
        self.square = ops.Square()
        self.sqrt = ops.Sqrt()

    def construct(self, x):
        if self.data_format == "channels_last":
            return nn.LayerNorm((x.shape[-1],), epsilon=self.eps)(x)
        else:
            u = self.reduce_mean(x, 1)
            s = self.reduce_mean(self.square(x - u), 1)
            x = (x - u) / self.sqrt(s + self.eps)
            x = self.weight.reshape(1, -1, 1, 1) * x + self.bias.reshape(1, -1, 1, 1)
            return x

# Model variants (register_model decorators can be removed or adapted as needed)


def conv2former_n(pretrained=False, **kwargs):
    model = Conv2Former(kernel_size=7, dims=[64, 128, 256, 512], mlp_ratios=[4, 4, 4, 4], depths=[2, 2, 8, 2], **kwargs)
    return model


def conv2former_t(pretrained=False, **kwargs):
    model = Conv2Former(kernel_size=11, dims=[72, 144, 288, 576], mlp_ratios=[4, 4, 4, 4], depths=[3, 3, 12, 3], **kwargs)
    return model


def conv2former_s(pretrained=False, **kwargs):
    model = Conv2Former(kernel_size=11, dims=[72, 144, 288, 576], mlp_ratios=[4, 4, 4, 4], depths=[4, 4, 32, 4], **kwargs)
    return model


def conv2former_b(pretrained=False, **kwargs):
    model = Conv2Former(kernel_size=11, dims=[96, 192, 384, 768], mlp_ratios=[4, 4, 4, 4], depths=[4, 4, 34, 4], **kwargs)
    return model


def conv2former_b_22k(pretrained=False, **kwargs):
    model = Conv2Former(kernel_size=7, dims=[96, 192, 384, 768], mlp_ratios=[4, 4, 4, 4], depths=[4, 4, 34, 4], **kwargs)
    return model


def conv2former_l(pretrained=False, **kwargs):
    model = Conv2Former(kernel_size=11, dims=[128, 256, 512, 1024], mlp_ratios=[4, 4, 4, 4], depths=[4, 4, 48, 4], **kwargs)
    return model

# 初始化设置
ms.set_seed(42)

_logger = logging.getLogger('train')

def parse_args():
    parser = argparse.ArgumentParser(description='MindSpore ImageNet Training')
    # 数据集参数
    parser.add_argument('data_dir', metavar='DIR', help='path to dataset')
    parser.add_argument('-b', '--batch-size', type=int, default=128, help='input batch size')
    
    # 模型参数
    parser.add_argument('--model', default='conv2former_n', type=str, help='model name')
    parser.add_argument('--num-classes', type=int, default=1000, help='number of classes')
    
    # 优化器参数
    parser.add_argument('--lr', type=float, default=1e-3, help='learning rate')
    parser.add_argument('--weight-decay', type=float, default=0.05, help='weight decay')
    
    # 训练参数
    parser.add_argument('--epochs', type=int, default=300, help='number of epochs')
    parser.add_argument('--distributed', action='store_true', help='enable distributed training')
    
    return parser.parse_args()

def create_dataset(data_dir, batch_size=128, training=True):
    # 数据预处理
    mean = [0.485 * 255, 0.456 * 255, 0.406 * 255]
    std = [0.229 * 255, 0.224 * 255, 0.225 * 255]
    
    transform = [
        vision.Decode(),
        vision.Resize(256),
        vision.RandomCrop(224),
        vision.Normalize(mean, std),
        vision.HWC2CHW()
    ]
    
    dataset = ImageFolderDataset(data_dir, shuffle=True)
    dataset = dataset.map(transform, input_columns="image")
    dataset = dataset.batch(batch_size)
    return dataset

def main():
    args = parse_args()
    
    # 分布式初始化
    if args.distributed:
        init()
        rank = get_rank()
        group_size = get_group_size()
    else:
        rank = 0
        group_size = 1

    # 创建模型
    model_dict = {
        'conv2former_n': conv2former_n(num_classes=args.num_classes),
        'conv2former_t': conv2former_t(num_classes=args.num_classes),
        'conv2former_s': conv2former_s(num_classes=args.num_classes),
        'conv2former_b': conv2former_b(num_classes=args.num_classes),
        'conv2former_b_22k': conv2former_b_22k(num_classes=args.num_classes),
        'conv2former_l': conv2former_l(num_classes=args.num_classes)
    }
    model = model_dict.get(args.model)
    if model is None:
        raise ValueError(f"Unsupported model: {args.model}")
    
    # 数据加载
    train_dataset = create_dataset(args.data_dir, args.batch_size)
    
    # 优化器
    optimizer = nn.AdamWeightDecay(
        model.trainable_params(),
        learning_rate=args.lr,
        weight_decay=args.weight_decay
    )
    
    # 损失函数
    loss_fn = nn.SoftmaxCrossEntropyWithLogits(sparse=True, reduction='mean')
    
    # 训练模型
    model = Model(model, loss_fn, optimizer, metrics={'acc'})
    
    # 回调函数
    callbacks = [
        LossMonitor(),
        TimeMonitor(),
    ]
    
    # 开始训练
    model.train(args.epochs, train_dataset, callbacks=callbacks, dataset_sink_mode=True)

if __name__ == '__main__':
    main()
