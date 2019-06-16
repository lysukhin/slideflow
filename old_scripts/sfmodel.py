# Copyright (C) James Dolezal - All Rights Reserved
#
# Unauthorized copying of this file, via any medium is strictly prohibited
# Proprietary and confidential
# Written by James Dolezal <jamesmdolezal@gmail.com>, October 2017
# ==========================================================================

# Update 3/2/2019: Beginning tf.data implementation
# Update 5/29/2019: Supports both loose image tiles and TFRecords, 
#   annotations supplied by separate annotation file upon initial model call

''''Builds a CNN model.'''

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import os
import sys
from datetime import datetime

import numpy as np
import pickle
import argparse

import tensorflow as tf
from tensorflow.contrib.framework import arg_scope
from tensorflow.summary import FileWriterCache
from tensorboard import summary as summary_lib
from tensorboard.plugins.custom_scalar import layout_pb2
import tensorflow.contrib.lookup

import inception_v4
from inception_utils import inception_arg_scope
from glob import glob
from scipy.stats import linregress

from util import tfrecords, sfutil
from util.sfutil import TCGAAnnotations

slim = tf.contrib.slim

RUN_OPTS = tf.RunOptions(report_tensor_allocations_upon_oom = True)

# Calculate accuracy with https://stackoverflow.com/questions/50111438/tensorflow-validate-accuracy-with-batch-data
# TODO: try next, comment out line 254 (results in calculating total_loss before update_ops is called)
# TODO: visualize graph, memory usage, and compute time with https://www.tensorflow.org/guide/graph_viz
# TODO: export logs to file for monitoring remotely

class SFModelConfig:
	def __init__(self, image_size, num_classes, batch_size, augment=False, learning_rate=0.01, 
				beta1=0.9, beta2=0.999, epsilon=1.0, batch_norm_decay=0.99, early_stop=0.015, 
				max_epoch=300, log_frequency=20, summary_steps=20, test_frequency=600, use_fp16=True):
		''' Declare constants describing the model and training process.
		Args:
			image_size						Size of input images in pixels
			num_classes						Number of classes
			batch_size						Batch size for training
			augment							Whether or not to perform data augmentation
			learning_rate					Learning rate for the Adams Optimizer
			beta1							Beta1 for AdamOptimizer
			beta2							Beta2 for AdamOptimizer
			epsilon							Epsilon for AdamOptimizer
			batch_norm_decay				Decay rate for batch_norm (0.999 default, use lower numbers if poor validation performance)
			early_stop						Rate of validation loss decay that should trigger early stopping
			max_epoch						Maximum number of times to repeat through training set
			log_frequency					How often to log results to console, in steps
			summary_steps					How often to save summaries for Tensorboard display, in steps
			test_frequency					How often to run validation testing, in steps
			use_fp16						Whether to use FP16 or not (vs. FP32)
		'''		
		self.image_size = image_size
		self.num_classes = num_classes
		self.batch_size = batch_size
		self.augment = augment
		self.learning_rate = learning_rate
		self.beta1 = beta1
		self.beta2 = beta2
		self.epsilon = epsilon
		self.batch_norm_decay = batch_norm_decay
		self.early_stop = early_stop
		self.max_epoch = max_epoch
		self.log_frequency = log_frequency
		self.summary_steps = summary_steps
		self.test_frequency = test_frequency
		self.use_fp16 = use_fp16

	def get_args(self):
		ignored_args = ['image_size', 'num_classes', 'batch_size', 'use_fp16', 'get_args', 'print_config']
		return [i for i in dir(self) if (not i[0]=='_') and (i not in ignored_args)]

	def print_config(self):
		print(f" + [{sfutil.info('INFO')}] Model configuration:")
		for arg in self.get_args():
			value = getattr(self, arg)
			print(f"   - {sfutil.header(arg)} = {value}")

class SlideflowModel:
	''' Model containing all functions necessary to build input dataset pipelines,
	build a training and validation set model, and monitor and execute training.'''

	def __init__(self, data_directory, input_directory, annotations_file):
		self.DATA_DIR = data_directory
		self.INPUT_DIR = input_directory
		self.MODEL_DIR = self.DATA_DIR # Directory where to write event logs and checkpoints.
		self.TRAIN_DIR = os.path.join(self.MODEL_DIR, 'train') # Directory where to write eval logs and summaries.
		self.TEST_DIR = os.path.join(self.MODEL_DIR, 'test') # Directory where to write eval logs and summaries.
		self.TRAIN_FILES = os.path.join(self.INPUT_DIR, "train_data/*/*.jpg")
		self.TEST_FILES = os.path.join(self.INPUT_DIR, "eval_data/*/*.jpg")
		self.TRAIN_TFRECORD = os.path.join(self.INPUT_DIR, "train.tfrecords")
		self.EVAL_TFRECORD = os.path.join(self.INPUT_DIR, "eval.tfrecords")
		self.USE_TFRECORD = (os.path.exists(self.TRAIN_TFRECORD) and os.path.exists(self.EVAL_TFRECORD))

		annotations = sfutil.get_annotations_dict(annotations_file, key_name="slide", value_name="category")
		# TODO: use verification done by parent slideflow module; if not done, offer to use again
		#tfrecord_files = [self.TRAIN_TFRECORD, self.EVAL_TFRECORD] if self.USE_TFRECORD else []
		#sfutil.verify_tiles(annotations, self.INPUT_DIR, tfrecord_files)

		# Reset default graph
		tf.reset_default_graph()

		with tf.device('/cpu'):
			with tf.variable_scope("annotations"):
				self.ANNOTATIONS_TABLE = tf.contrib.lookup.HashTable(
					tf.contrib.lookup.KeyValueTensorInitializer(list(annotations.keys()), list(annotations.values())), -1
				)

		if tf.gfile.Exists(self.MODEL_DIR):
			tf.gfile.DeleteRecursively(self.MODEL_DIR)
		tf.gfile.MakeDirs(self.MODEL_DIR)

	def config(self, config):
		self.IMAGE_SIZE = config.image_size
		self.NUM_CLASSES = config.num_classes
		self.BATCH_SIZE = config.batch_size
		self.AUGMENT = config.augment
		self.LEARNING_RATE = config.learning_rate
		self.BETA1 = config.beta1
		self.BETA2 = config.beta2
		self.EPSILON = config.epsilon
		self.BATCH_NORM_DECAY = config.batch_norm_decay
		self.VALIDATION_EARLY_STOP_SLOPE = config.early_stop
		self.MAX_EPOCH = config.max_epoch
		self.LOG_FREQUENCY = config.log_frequency
		self.SUMMARY_STEPS = config.summary_steps
		self.TEST_FREQUENCY = config.test_frequency
		self.USE_FP16 = config.use_fp16
		self.DTYPE = tf.float16 if self.USE_FP16 else tf.float32
		config.print_config()

	def _gen_filenames_op(self, dir_string):
		filenames_op = tf.train.match_filenames_once(dir_string)
		labels_op = tf.map_fn(lambda f: self.ANNOTATIONS_TABLE.lookup(tf.string_split([f], '/').values[tf.constant(-2, dtype=tf.int32)]),
								filenames_op, dtype=tf.int32)
		return filenames_op, labels_op

	def _process_image(self, image_string):
		image = tf.image.decode_jpeg(image_string, channels = 3)
		image = tf.image.per_image_standardization(image)

		if self.AUGMENT:
			# Apply augmentations
			# Rotate 0, 90, 180, 270 degrees
			image = tf.image.rot90(image, tf.random_uniform(shape=[], minval=0, maxval=4, dtype=tf.int32))

			# Random flip and rotation
			image = tf.image.random_flip_left_right(image)
			image = tf.image.random_flip_up_down(image)

		dtype = tf.float16 if self.USE_FP16 else tf.float32
		image = tf.image.convert_image_dtype(image, dtype)
		image.set_shape([self.IMAGE_SIZE, self.IMAGE_SIZE, 3])
		return image

	def _parse_function(self, filename, label):
		image_string = tf.read_file(filename)
		image = self._process_image(image_string)
		return image, label

	def _parse_tfrecord_function(self, tfrecord_features):
		case = tfrecord_features['case']
		label = self.ANNOTATIONS_TABLE.lookup(case)
		image_string = tfrecord_features['image_raw']
		image = self._process_image(image_string)
		return image, label

	def _gen_batched_dataset(self, filenames, labels):
		# Replace the below dataset with one that uses a Python generator for flexibility of labeling
		dataset = tf.data.Dataset.from_tensor_slices((filenames, labels))
		dataset = dataset.shuffle(tf.size(filenames, out_type=tf.int64))
		dataset = dataset.map(self._parse_function, num_parallel_calls = 8)
		dataset = dataset.batch(self.BATCH_SIZE)
		return dataset

	def _gen_batched_dataset_from_tfrecord(self, tfrecord):
		raw_image_dataset = tf.data.TFRecordDataset(tfrecord)
		feature_description = tfrecords.FEATURE_DESCRIPTION

		def _parse_image_function(example_proto):
			"""Parses the input tf.Example proto using the above feature dictionary."""
			return tf.parse_single_example(example_proto, feature_description)

		dataset = raw_image_dataset.map(_parse_image_function)
		dataset = dataset.shuffle(100000)
		dataset = dataset.map(self._parse_tfrecord_function, num_parallel_calls = 8)
		dataset = dataset.batch(self.BATCH_SIZE)
		return dataset

	def build_inputs(self):
		'''Construct input for the model.

		Args:
			sess: active tensorflow session

		Returns:
			next_batch_images: Images. 4D tensor of [batch_size, IMAGE_SIZE, IMAGE_SIZE, 3] size.
			next_batch_labels: Labels. 1D tensor of [batch_size] size.
		'''

		if not self.USE_TFRECORD:
			with tf.name_scope('filename_input'):
				train_filenames_op, train_labels_op = self._gen_filenames_op(self.TRAIN_FILES)
				test_filenames_op, test_labels_op = self._gen_filenames_op(self.TEST_FILES)
			train_dataset = self._gen_batched_dataset(train_filenames_op, train_labels_op)
			test_dataset = self._gen_batched_dataset(test_filenames_op, test_labels_op)
		else:
			with tf.name_scope('input'):
				train_dataset = self._gen_batched_dataset_from_tfrecord(self.TRAIN_TFRECORD)
				test_dataset = self._gen_batched_dataset_from_tfrecord(self.EVAL_TFRECORD)
		with tf.name_scope('input'):
			train_dataset = train_dataset.repeat(self.MAX_EPOCH)
			train_dataset = train_dataset.prefetch(1)
			test_dataset = test_dataset.prefetch(1)

			with tf.name_scope('iterator'):
				train_iterator = train_dataset.make_initializable_iterator()

				# Will likely need to be re-initializable iterator to repeat testing
				test_iterator = test_dataset.make_initializable_iterator()

				handle = tf.placeholder(tf.string, shape=[])
				iterator = tf.data.Iterator.from_string_handle(handle, 
															   train_iterator.output_types,
															   train_iterator.output_shapes)

			next_batch_images, next_batch_labels = iterator.get_next()

			if self.USE_FP16: next_batch_images = tf.cast(next_batch_images, dtype=tf.float16)	

		return next_batch_images, next_batch_labels, train_iterator, test_iterator, handle

	def loss(self, logits, labels):
		# Calculate average cross entropy loss across the batch.
		labels = tf.cast(labels, tf.int64)
		cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
			labels=labels, logits=logits, name='cross_entropy_per_example')
		cross_entropy_mean = tf.reduce_mean(cross_entropy, name='cross_entropy')
		tf.add_to_collection('losses', cross_entropy_mean)

		# Total loss is defined as the cross entropy loss plus all of the weight decay terms (L2 loss)
		return tf.add_n(tf.get_collection('losses'), name='total_loss')

	def generate_loss_chart(self):
		return summary_lib.custom_scalar_pb(
			layout_pb2.Layout(category=[
				layout_pb2.Category(
					title='losses',
					chart=[
						layout_pb2.Chart(
							title='losses',
							multiline=layout_pb2.MultilineChartContent(tag=[
								'loss/training', 'loss/valid'
							]))
					])
			]))

	def build_train_op(self, total_loss, global_step):
		opt = tf.train.AdamOptimizer(learning_rate=self.LEARNING_RATE,
										beta1=self.BETA1,
										beta2=self.BETA2,
										epsilon=self.EPSILON)
		train_op = slim.learning.create_train_op(total_loss, opt)
		return train_op

	def train(self, retrain_model=None, retrain_weights=None, restore_checkpoint=None):
		'''Train the model for a number of steps, according to flags set by the argument parser.'''
		
		if restore_checkpoint:
			ckpt = tf.train.get_checkpoint_state(restore_checkpoint)
	
		variables_to_ignore = []#("InceptionV4/Logits/Logits/weights:0", "InceptionV4/Logits/Logits/biases:0")
		variables_to_restore = []
		assign_ops = []
		global_step = tf.train.get_or_create_global_step()

		with tf.device('/cpu'):
			print(f" + [{sfutil.info('INFO')}] Assembling input pipeline... ", end="", flush=True)
			next_batch_images, next_batch_labels, train_it, test_it, it_handle = self.build_inputs()
			print("complete.")
		
		training_pl = tf.placeholder(tf.bool, name='train_pl')
		with arg_scope(inception_arg_scope()):
			print(f" + [{sfutil.info('INFO')}] Assembling model graph... ", end="", flush=True)
			logits, end_points = inception_v4.inception_v4(next_batch_images, 
														   num_classes=self.NUM_CLASSES,
														   is_training=training_pl,
														   reuse=tf.AUTO_REUSE,
														   batch_norm_decay=self.BATCH_NORM_DECAY)
			print('complete.')
			if restore_checkpoint:
				print(f" + [{sfutil.info('INFO')}] Restoring checkpoint... ", end="", flush=True)
				for trainable_var in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES):
					if (trainable_var.name not in variables_to_ignore) and (trainable_var.name[12:21] != "AuxLogits"):
						variables_to_restore.append(trainable_var)
			
			if retrain_weights:
				print(f" + [{sfutil.info('INFO')}] Restoring pretrained weights... ", end="", flush=True)
				for variable in tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES):
					if variable.name in retrain_weights:
						print(f"Found: {variable.name}")
						assign_op = variable.assign(retrain_weights[variable.name])
						assign_ops.append(assign_op)
					else:
						print(f"Not found: {variable.name}")
			print("complete.")

		loss = self.loss(logits, next_batch_labels)

		# Create an averaging op to follow validation accuracy
		with tf.name_scope('mean_validation_loss'):
			validation_loss, validation_loss_update = tf.metrics.mean(loss)
			stream_vars = [v for v in tf.local_variables() if v.name.startswith('mean_validation_loss')]
			stream_vars_reset = [v.initializer for v in stream_vars]
		print(f" + [{sfutil.info('INFO')}] Assembling train op... ", end="", flush=True)
		train_op = self.build_train_op(loss, global_step)
		print('complete.')
		# -- SUMMARIES -----------------------------------------------------------------------------
		
		with tf.name_scope('loss'):
			train_summ = summary_lib.scalar('training', loss)
			inception_summaries = tf.summary.merge_all()
			valid_summ = summary_lib.scalar('valid', validation_loss)
		
		layout_summary = self.generate_loss_chart()
		
		init = (tf.global_variables_initializer(), tf.local_variables_initializer())

		class _LoggerHook(tf.train.SessionRunHook):
			'''Logs loss and runtime.'''
			def __init__(self, train_str, test_str, parent):
				self.parent = parent
				self.train_str = train_str
				self.test_str = test_str
				self.train_handle = None
				self.test_handle = None

			def after_create_session(self, session, coord):
				if self.train_str is not None:
					self.train_iterator_handle, self.test_iterator_handle = session.run([self.train_str, self.test_str])
					session.run([init, train_it.initializer, test_it.initializer])
					
			def begin(self):
				self._step = -1
				self._start_time = time.time()

			def before_run(self, run_context):
				feed_dict = run_context.original_args.feed_dict
				if feed_dict and it_handle in feed_dict and feed_dict[it_handle] == self.train_iterator_handle:
					self._step += 1
					return tf.train.SessionRunArgs(loss)

			def after_run(self, run_context, run_values):
				'''Log loss results for training sessions to the console'''
				if ((self._step % self.parent.LOG_FREQUENCY == 0) and
					(run_context.original_args.feed_dict) and
					(it_handle in run_context.original_args.feed_dict) and
					(run_context.original_args.feed_dict[it_handle] == self.train_iterator_handle)):

					current_time = time.time()
					duration = current_time - self._start_time
					self._start_time = current_time
					loss_value = run_values.results
					images_per_sec = self.parent.LOG_FREQUENCY * self.parent.BATCH_SIZE / duration
					sec_per_batch = float(duration / self.parent.LOG_FREQUENCY)
					format_str = ('%s: step %d, loss = %.2f (%.1f images/sec; %.3f sec/batch)')
					print(format_str % (datetime.now(), self._step, loss_value,
										images_per_sec, sec_per_batch))
		
		loggerhook = _LoggerHook(train_it.string_handle(), test_it.string_handle(), self)
		validation_losses = []
		first_validation_loss = None
		step = 1
		
		if restore_checkpoint:
			pretrained_saver = tf.train.Saver(variables_to_restore)
		print(f" + [{sfutil.info('INFO')}] Initializing training session... ")
		with tf.train.MonitoredTrainingSession(
			checkpoint_dir = self.MODEL_DIR,
			hooks = [loggerhook], #tf.train.NanTensorHook(loss),
			config = tf.ConfigProto(
					log_device_placement=False),
			save_summaries_steps = None, #self.SUMMARY_STEPS,
			save_summaries_secs = None) as mon_sess:

			print("complete.")

			test_writer = tf.summary.FileWriter(self.TEST_DIR, mon_sess.graph)
			train_writer = FileWriterCache.get(self.TRAIN_DIR) # SummaryWriterCache
			train_writer.add_summary(layout_summary)
			

			if restore_checkpoint and ckpt and ckpt.model_checkpoint_path:
				print(f" + [{sfutil.info('INFO')}] Restoring checkpoint...")
				pretrained_saver.restore(mon_sess, ckpt.model_checkpoint_path)

			if retrain_weights:
				print(f" + [{sfutil.info('INFO')}] Loading pre-trained weights into graph... ", end="", flush=True)
				mon_sess.run(assign_ops)
				print("complete.")

			while not mon_sess.should_stop():
				if (step % self.SUMMARY_STEPS == 0):
					_, merged, step = mon_sess.run([train_op, inception_summaries, global_step], feed_dict={it_handle:loggerhook.train_iterator_handle,
																											training_pl:True}, options=RUN_OPTS)
					train_writer.add_summary(merged, step)
				else:
					_, step = mon_sess.run([train_op, global_step], feed_dict={it_handle:loggerhook.train_iterator_handle,
																										training_pl:True}, options=RUN_OPTS)
				if (step % self.TEST_FREQUENCY == 0):
					print(" + Validation testing...")
					# Reset the validation loss streaming variables (variables which keep track of the average
					#   loss across the entire validation dataset)
					mon_sess.run(stream_vars_reset, feed_dict={it_handle:loggerhook.test_iterator_handle,
															   training_pl:False}, options=RUN_OPTS)
					while True:
						try:
							_, val_acc = mon_sess.run([validation_loss_update, validation_loss], feed_dict={it_handle:loggerhook.test_iterator_handle,
																											training_pl:False}, options=RUN_OPTS)
						except tf.errors.OutOfRangeError:
							break

					# Write results to summaries and console
					summ = mon_sess.run(valid_summ)
					test_writer.add_summary(summ, step)
					print(" + Validation loss: {}".format(val_acc))

					# Request an early stop if the the validation loss is less than the first run (as there is often
					#   a rise in validation loss before convergence occurs) and the average validation loss drop
					#   is less than criteria (e.g. -0.025 loss / run)
					validation_losses = validation_losses[-10:] + [val_acc]
					if not first_validation_loss:
						first_validation_loss = val_acc
					if val_acc < first_validation_loss and len(validation_losses) == 10:
						ys = np.array(validation_losses)
						xs = np.array(range(len(validation_losses)))
						slope = linregress(xs, ys).slope
						early_stop_slope = -1 * first_validation_loss * self.VALIDATION_EARLY_STOP_SLOPE
						if slope > early_stop_slope:
							print(f" + Early stop detected: slope {slope}, criteria: {early_stop_slope}")
							break

					# Reset the test iterator initializer for the next run
					mon_sess.run(test_it.initializer, feed_dict={it_handle:loggerhook.test_iterator_handle}, options=RUN_OPTS)
					loggerhook._start_time = time.time()

		return validation_losses[-1]

	def retrain(self, model=None, weights=None, restore_checkpoint=None):
		if model == None: model = '/home/shawarma/thyroid/models/inception_v4_2018_04_27/inception_v4.pb'
		if weights == None: weights = '/home/shawarma/thyroid/thyroid/obj/inception_v4_imagenet_pretrained.pkl'
		with open(weights, 'rb') as f:
			var_dict = pickle.load(f)
		
		self.train(retrain_model=None, retrain_weights=var_dict, restore_checkpoint=restore_checkpoint)

if __name__ == "__main__":
	#os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
	#tf.logging.set_verbosity(tf.logging.ERROR)

	parser = argparse.ArgumentParser(description = "Train a CNN using an Inception-v4 network")
	parser.add_argument('-d', '--dir', help='Path to root directory for saving model.')
	parser.add_argument('-i', '--input', help='Path to root directory with training and eval data.')
	parser.add_argument('-r', '--retrain', help='Path to directory containing model to use as pretraining')
	parser.add_argument('-a', '--annotation', help='Path to root directory with training and eval data.')
	args = parser.parse_args()

	#SFM = SlideflowModel(args.dir, args.input, args.annotation)
	#model_config = SFModelConfig(args.size, args.classes, args.batch, augment=True, use_fp16=args.use_fp16)
	#SFM.config(model_config)
	#SFM.train(restore_checkpoint = args.retrain)