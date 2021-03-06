# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""SSD MNASNet FPN Feature Extractor."""

import copy
import functools
import tensorflow as tf

from object_detection.meta_architectures import ssd_meta_arch
from object_detection.models import feature_map_generators
from object_detection.utils import context_manager
from object_detection.utils import ops
from object_detection.utils import shape_utils
from nets import mnasnet

slim = tf.contrib.slim


class SSDMNASNetA1FpnFeatureExtractor(ssd_meta_arch.SSDFeatureExtractor):
  """SSD Feature Extractor using MNASNet a1 FPN features."""

  def __init__(self,
               is_training,
               depth_multiplier,
               min_depth,
               pad_to_multiple,
               conv_hyperparams_fn,
               fpn_min_level=3,
               fpn_max_level=7,
               additional_layer_depth=256,
               reuse_weights=None,
               use_explicit_padding=False,
               use_depthwise=False,
               override_base_feature_extractor_hyperparams=False):
    """SSD FPN feature extractor based on MNASNet a1 architecture.

    Args:
      is_training: whether the network is in training mode.
      depth_multiplier: float depth multiplier for feature extractor.
      min_depth: minimum feature extractor depth.
      pad_to_multiple: the nearest multiple to zero pad the input height and
        width dimensions to.
      conv_hyperparams_fn: A function to construct tf slim arg_scope for conv2d
        and separable_conv2d ops in the layers that are added on top of the base
        feature extractor.
      fpn_min_level: the highest resolution feature map to use in FPN. The valid
        values are {2, 3, 4, 5} which map to Mnasnet a1 layers
        {layer_5, layer_8, layer_13, layer_19}, respectively.
      fpn_max_level: the smallest resolution feature map to construct or use in
        FPN. FPN constructions uses features maps starting from fpn_min_level
        upto the fpn_max_level. In the case that there are not enough feature
        maps in the backbone network, additional feature maps are created by
        applying stride 2 convolutions until we get the desired number of fpn
        levels.
      additional_layer_depth: additional feature map layer channel depth.
      reuse_weights: whether to reuse variables. Default is None.
      use_explicit_padding: Whether to use explicit padding when extracting
        features. Default is False.
      use_depthwise: Whether to use depthwise convolutions. Default is False.
      override_base_feature_extractor_hyperparams: Whether to override
        hyperparameters of the base feature extractor with the one from
        `conv_hyperparams_fn`.
    """
    super(SSDMNASNetA1FpnFeatureExtractor, self).__init__(
        is_training=is_training,
        depth_multiplier=depth_multiplier,
        min_depth=min_depth,
        pad_to_multiple=pad_to_multiple,
        conv_hyperparams_fn=conv_hyperparams_fn,
        reuse_weights=reuse_weights,
        use_explicit_padding=use_explicit_padding,
        use_depthwise=use_depthwise,
        override_base_feature_extractor_hyperparams=
        override_base_feature_extractor_hyperparams)
    self._fpn_min_level = fpn_min_level
    self._fpn_max_level = fpn_max_level
    self._additional_layer_depth = additional_layer_depth
    self._conv_defs = mnasnet.MNASNET_A1_DEF

  def preprocess(self, resized_inputs):
    """SSD preprocessing.

    Maps pixel values to the range [-1, 1].

    Args:
      resized_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.

    Returns:
      preprocessed_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.
    """
    return (2.0 / 255.0) * resized_inputs - 1.0

  def extract_features(self, preprocessed_inputs):
    """Extract features from preprocessed inputs.

    Args:
      preprocessed_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.

    Returns:
      feature_maps: a list of tensors where the ith tensor has shape
        [batch, height_i, width_i, depth_i]
    """
    preprocessed_inputs = shape_utils.check_min_image_dim(
        33, preprocessed_inputs)

    with tf.variable_scope('Mnasnet', reuse=self._reuse_weights) as scope:
      with slim.arg_scope(
          mnasnet.training_scope(is_training=None, bn_decay=0.9997)), \
          slim.arg_scope(
              [mnasnet.depth_multiplier_fn], min_depth=self._min_depth):
        with (slim.arg_scope(self._conv_hyperparams_fn())
              if self._override_base_feature_extractor_hyperparams else
              context_manager.IdentityContextManager()):
          _, image_features = mnasnet.mnasnet_base(
              ops.pad_to_multiple(preprocessed_inputs, self._pad_to_multiple),
              final_endpoint='layer_18',
              depth_multiplier=self._depth_multiplier,
              conv_defs=self._conv_defs,
              use_explicit_padding=self._use_explicit_padding,
              scope=scope)
      depth_fn = lambda d: max(int(d * self._depth_multiplier), self._min_depth)
      with slim.arg_scope(self._conv_hyperparams_fn()):
        with tf.variable_scope('fpn', reuse=self._reuse_weights):
          feature_blocks = [
              'layer_4', 'layer_7', 'layer_13', 'layer_18'
          ]
          base_fpn_max_level = min(self._fpn_max_level, 5)
          feature_block_list = []
          for level in range(self._fpn_min_level, base_fpn_max_level + 1):
            feature_block_list.append(feature_blocks[level - 2])
          fpn_features = feature_map_generators.fpn_top_down_feature_maps(
              [(key, image_features[key]) for key in feature_block_list],
              depth=depth_fn(self._additional_layer_depth),
              use_depthwise=self._use_depthwise,
              use_explicit_padding=self._use_explicit_padding)
          feature_maps = []
          for level in range(self._fpn_min_level, base_fpn_max_level + 1):
            feature_maps.append(fpn_features['top_down_{}'.format(
                feature_blocks[level - 2])])
          last_feature_map = fpn_features['top_down_{}'.format(
              feature_blocks[base_fpn_max_level - 2])]
          # Construct coarse features
          padding = 'VALID' if self._use_explicit_padding else 'SAME'
          kernel_size = 3
          for i in range(base_fpn_max_level + 1, self._fpn_max_level + 1):
            if self._use_depthwise:
              conv_op = functools.partial(
                  slim.separable_conv2d, depth_multiplier=1)
            else:
              conv_op = slim.conv2d
            if self._use_explicit_padding:
              last_feature_map = ops.fixed_padding(
                  last_feature_map, kernel_size)
            last_feature_map = conv_op(
                last_feature_map,
                num_outputs=depth_fn(self._additional_layer_depth),
                kernel_size=[kernel_size, kernel_size],
                stride=2,
                padding=padding,
                scope='bottom_up_Conv2d_{}'.format(i - base_fpn_max_level + 19))
            feature_maps.append(last_feature_map)
    return feature_maps

class SSDMNASNetB1FpnFeatureExtractor(ssd_meta_arch.SSDFeatureExtractor):
  """SSD Feature Extractor using MNASNet b1 FPN features."""

  def __init__(self,
               is_training,
               depth_multiplier,
               min_depth,
               pad_to_multiple,
               conv_hyperparams_fn,
               fpn_min_level=3,
               fpn_max_level=7,
               additional_layer_depth=256,
               reuse_weights=None,
               use_explicit_padding=False,
               use_depthwise=False,
               override_base_feature_extractor_hyperparams=False):
    """SSD FPN feature extractor based on MNASNet b1 architecture.

    Args:
      is_training: whether the network is in training mode.
      depth_multiplier: float depth multiplier for feature extractor.
      min_depth: minimum feature extractor depth.
      pad_to_multiple: the nearest multiple to zero pad the input height and
        width dimensions to.
      conv_hyperparams_fn: A function to construct tf slim arg_scope for conv2d
        and separable_conv2d ops in the layers that are added on top of the base
        feature extractor.
      fpn_min_level: the highest resolution feature map to use in FPN. The valid
        values are {2, 3, 4, 5} which map to Mnasnet b1 layers
        {layer_5, layer_8, layer_13, layer_19}, respectively.
      fpn_max_level: the smallest resolution feature map to construct or use in
        FPN. FPN constructions uses features maps starting from fpn_min_level
        upto the fpn_max_level. In the case that there are not enough feature
        maps in the backbone network, additional feature maps are created by
        applying stride 2 convolutions until we get the desired number of fpn
        levels.
      additional_layer_depth: additional feature map layer channel depth.
      reuse_weights: whether to reuse variables. Default is None.
      use_explicit_padding: Whether to use explicit padding when extracting
        features. Default is False.
      use_depthwise: Whether to use depthwise convolutions. Default is False.
      override_base_feature_extractor_hyperparams: Whether to override
        hyperparameters of the base feature extractor with the one from
        `conv_hyperparams_fn`.
    """
    super(SSDMNASNetB1FpnFeatureExtractor, self).__init__(
        is_training=is_training,
        depth_multiplier=depth_multiplier,
        min_depth=min_depth,
        pad_to_multiple=pad_to_multiple,
        conv_hyperparams_fn=conv_hyperparams_fn,
        reuse_weights=reuse_weights,
        use_explicit_padding=use_explicit_padding,
        use_depthwise=use_depthwise,
        override_base_feature_extractor_hyperparams=
        override_base_feature_extractor_hyperparams)
    self._fpn_min_level = fpn_min_level
    self._fpn_max_level = fpn_max_level
    self._additional_layer_depth = additional_layer_depth
    self._conv_defs = mnasnet.MNASNET_B1_DEF

  def preprocess(self, resized_inputs):
    """SSD preprocessing.

    Maps pixel values to the range [-1, 1].

    Args:
      resized_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.

    Returns:
      preprocessed_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.
    """
    return (2.0 / 255.0) * resized_inputs - 1.0

  def extract_features(self, preprocessed_inputs):
    """Extract features from preprocessed inputs.

    Args:
      preprocessed_inputs: a [batch, height, width, channels] float tensor
        representing a batch of images.

    Returns:
      feature_maps: a list of tensors where the ith tensor has shape
        [batch, height_i, width_i, depth_i]
    """
    preprocessed_inputs = shape_utils.check_min_image_dim(
        33, preprocessed_inputs)

    with tf.variable_scope('Mnasnet', reuse=self._reuse_weights) as scope:
      with slim.arg_scope(
          mnasnet.training_scope(is_training=None, bn_decay=0.9997)), \
          slim.arg_scope(
              [mnasnet.depth_multiplier_fn], min_depth=self._min_depth):
        with (slim.arg_scope(self._conv_hyperparams_fn())
              if self._override_base_feature_extractor_hyperparams else
              context_manager.IdentityContextManager()):
          _, image_features = mnasnet.mnasnet_base(
              ops.pad_to_multiple(preprocessed_inputs, self._pad_to_multiple),
              final_endpoint='layer_19',
              depth_multiplier=self._depth_multiplier,
              conv_defs=self._conv_defs,
              use_explicit_padding=self._use_explicit_padding,
              scope=scope)
      depth_fn = lambda d: max(int(d * self._depth_multiplier), self._min_depth)
      with slim.arg_scope(self._conv_hyperparams_fn()):
        with tf.variable_scope('fpn', reuse=self._reuse_weights):
          feature_blocks = [
              'layer_5', 'layer_8', 'layer_13', 'layer_19'
          ]
          base_fpn_max_level = min(self._fpn_max_level, 5)
          feature_block_list = []
          for level in range(self._fpn_min_level, base_fpn_max_level + 1):
            feature_block_list.append(feature_blocks[level - 2])
          fpn_features = feature_map_generators.fpn_top_down_feature_maps(
              [(key, image_features[key]) for key in feature_block_list],
              depth=depth_fn(self._additional_layer_depth),
              use_depthwise=self._use_depthwise,
              use_explicit_padding=self._use_explicit_padding)
          feature_maps = []
          for level in range(self._fpn_min_level, base_fpn_max_level + 1):
            feature_maps.append(fpn_features['top_down_{}'.format(
                feature_blocks[level - 2])])
          last_feature_map = fpn_features['top_down_{}'.format(
              feature_blocks[base_fpn_max_level - 2])]
          # Construct coarse features
          padding = 'VALID' if self._use_explicit_padding else 'SAME'
          kernel_size = 3
          for i in range(base_fpn_max_level + 1, self._fpn_max_level + 1):
            if self._use_depthwise:
              conv_op = functools.partial(
                  slim.separable_conv2d, depth_multiplier=1)
            else:
              conv_op = slim.conv2d
            if self._use_explicit_padding:
              last_feature_map = ops.fixed_padding(
                  last_feature_map, kernel_size)
            last_feature_map = conv_op(
                last_feature_map,
                num_outputs=depth_fn(self._additional_layer_depth),
                kernel_size=[kernel_size, kernel_size],
                stride=2,
                padding=padding,
                scope='bottom_up_Conv2d_{}'.format(i - base_fpn_max_level + 19))
            feature_maps.append(last_feature_map)
    return feature_maps
