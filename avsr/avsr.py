import tensorflow as tf
import collections
from .io_utils import make_iterator_from_one_record, create_unit_dict, make_iterator_from_two_records
from .utils import compute_wer, write_sequences_to_labelfile
from .video import cnn_layers
from .audio import process_audio
from .seq2seq import Seq2SeqModel
import time
from os import makedirs, path
from datetime import datetime
from pprint import pprint
import traceback

class Data(collections.namedtuple("Data", ("inputs", "inputs_length", "inputs_filenames",
                                           "labels", "labels_length", "labels_filenames",
                                           "iterator_initializer"))):
    pass


class Model(collections.namedtuple("Model",("data", "model", "initializer", "batch_size"))):
    pass


class AVSR(object):
    def __init__(self,
                 unit,
                 unit_file=None,
                 video_processing=None,
                 video_train_record=None,
                 video_test_record=None,
                 audio_processing=None,
                 audio_train_record=None,
                 audio_test_record=None,
                 labels_train_record=None,
                 labels_test_record=None,
                 batch_size=(12, 12),
                 cnn_filters=(48.0, 96.0, 128.0, 128.0, 192.0),
                 cnn_dense_units=128,
                 batch_normalisation=True,
                 input_dense_layers=(0,),
                 encoder_type='unidirectional',
                 cell_type='lstm',
                 recurrent_regularisation=0.0001,
                 encoder_units_per_layer=(256, 256, 256),
                 decoder_units_per_layer_am = (0,),
                 decoder_units_per_layer=(256,),
                 enable_attention=True,
                 attention_type=(('scaled_luong',)*1, ('scaled_luong')*1),
                 enable_attention_enc=True,
                 attention_type_enc={'audio': {
                                        's2s': (),
                                        'o2s': ('bahdanau')},
                                    'video': {
                                        's2s': (), 
                                        'o2s': ()}},
                 attention_output_enc={'audio': {
                                        's2s': (),
                                        'o2s': (False)},
                                    'video': {
                                        's2s': (), 
                                        'o2s': (True, True, True)}},
                 use_dropout=True,
                 dropout_probability=(0.9, 0.9, 0.9),
                 embedding_size=0,
                 sampling_probability_outputs=0.1,
                 label_skipping=False,
                 decoding_algorithm='beam_search',
                 beam_width=4,
                 optimiser='Adam',
                 learning_rate=0.001,
                 lr_decay='staircase',
                 clip_gradients=True,
                 max_gradient_norm=1.0,
                 num_gpus=1,
                 swap_memory=False,
                 write_attention_alignment=False,
                 dtype=tf.float32,
                 suppress_mode=False,
                 log_file=None
                 ):

        self._unit = unit
        self._unit_dict = create_unit_dict(unit_file=unit_file)

        self._video_processing = video_processing
        self._video_train_record = video_train_record
        self._video_test_record = video_test_record
        self._audio_processing = audio_processing
        self._audio_train_record = audio_train_record
        self._audio_test_record = audio_test_record
        self._labels_train_record = labels_train_record
        self._labels_test_record = labels_test_record
        self._suppress_mode=suppress_mode

        self._write_attention_alignment = write_attention_alignment

        self._hparams = tf.contrib.training.HParams(
            unit_dict=self._unit_dict,
            unit_file=unit_file,
            vocab_size=len(self._unit_dict),
            batch_size=batch_size,
            video_processing=video_processing,
            audio_processing=audio_processing,
            max_label_length={'viseme': 65, 'phoneme': 70, 'character': 80}[unit],  # max lens from tcdtimit
            batch_normalisation=batch_normalisation,
            input_dense_layers=input_dense_layers,
            cnn_filters=cnn_filters,
            cnn_dense_units=cnn_dense_units,
            encoder_type=encoder_type,
            cell_type=cell_type,
            recurrent_regularisation=recurrent_regularisation,
            encoder_units_per_layer=encoder_units_per_layer,
            decoder_units_per_layer=decoder_units_per_layer,
            decoder_units_per_layer_am=decoder_units_per_layer_am, 
            enable_attention=enable_attention,
            attention_type=attention_type,
            enable_attention_enc=enable_attention_enc,
            attention_type_enc=attention_type_enc,
            output_attention_enc=attention_output_enc,
            use_dropout=use_dropout,
            dropout_probability=dropout_probability,
            embedding_size=embedding_size,
            sampling_probability_outputs=sampling_probability_outputs,
            label_skipping=label_skipping,
            decoding_algorithm=decoding_algorithm,
            beam_width=beam_width,
            use_ctc=False,
            optimiser=optimiser,
            learning_rate=learning_rate,
            lr_decay=lr_decay,
            clip_gradients=clip_gradients,
            max_gradient_norm=max_gradient_norm,
            swap_memory=swap_memory,
            num_gpus=num_gpus,
            write_attention_alignment=write_attention_alignment,
            dtype=dtype
        )

        self._hparams_audio = tf.contrib.training.HParams(
            frame_length_msec=25,  # 25 > 20
            frame_step_msec=10,
            sample_rate=22050,
            fft_length=1024,
            mel_lower_edge_hz=80,
            mel_upper_edge_hz=22050 / 2,  # 11025 > 7600
            num_mel_bins=30,  # 30 > 60 > 80
            num_mfccs=26,  # 26 > 13
        )
        
        self._initialize_log(log_file)

        self._hparams_video = tf.contrib.training.HParams(
            cnn_filters=cnn_filters,
            cnn_dense_units=cnn_dense_units,
        )

        self._create_graphs()
        self._create_models()
        self._create_sessions()
        self._initialize_sessions()

    def __del__(self):
        traceback.print_stack()
        self._train_session.close()
        self._evaluate_session.close()
        self._predict_session.close()
        self._log_fd.flush()
        self._log_fd.close()

    def train(self,
              num_epochs=400,
              try_restore_latest_checkpoint=False
              ):
        log_file = self._log_fd.name
        checkpoint_dir = path.join('checkpoints', path.basename(log_file))
        checkpoint_path = path.join(checkpoint_dir, 'checkpoint.ckp')
        makedirs(path.dirname(checkpoint_dir), exist_ok=True)
        #makedirs(path.dirname(logfile), exist_ok=True)

        last_epoch = 0
        
        msg = 'try_restore_latest_checkpoint is {}\n'.format(
            try_restore_latest_checkpoint)
        self._log_fd.write(msg)
        print(msg)
        if try_restore_latest_checkpoint is True:
            try:
                latest_ckp = tf.train.latest_checkpoint(checkpoint_dir)
                last_epoch = int(latest_ckp.split('-')[-1])
                self._train_model.model.saver.restore(
                    sess=self._train_session,
                    save_path=latest_ckp, )
                msg = 'Restoring checkpoint from epoch {}\n'.format(last_epoch)
                self._log_fd.write(msg)
                print(msg)
            except Exception as e:
                msg = ('Could not restore from checkpoint {},' 
                       ' training from scratch!\n{}\n')
                msg = msg.format(checkpoint_path, e)
                print(msg)
                self._log_fd.write(msg)
        self._log_fd.flush()
        
        uer_best = 100 #can go beyond 100
        epoch_best = 0
        save_path = None
        save_path_best = None
        for current_epoch in range(1, num_epochs):
            epoch = last_epoch + current_epoch

            self._train_session.run([stream.iterator_initializer for stream in self._train_model.data
                                     if stream is not None])
            sum_loss = 0
            batches = 0

            start = time.time()

            try:
                while True:
                    out = self._train_session.run([self._train_model.model.train_op,
                                                   self._train_model.model.batch_loss,
                                                   ], )

                    sum_loss += out[1]
                    print('batch: {}'.format(batches))
                    batches += 1

            except tf.errors.OutOfRangeError:
                pass

            print('epoch time: {}'.format(time.time() - start))
            self._log_fd.write('Average loss at epoch {} is {}\n'.format(epoch, sum_loss / batches))
            self._log_fd.flush()
            
            if (epoch+1) % 10 == 0:
                save_path = self._train_model.model.saver.save(
                    sess=self._train_session,
                    save_path=checkpoint_path,
                    global_step=epoch,
                )

                error_rate = self.evaluate(save_path, epoch)
                if error_rate[self._unit] < uer_best:
                    uer_best = error_rate[self._unit]
                    epoch_best = epoch
                    save_path_best = save_path
        
        print('finish time: {:%Y-%m-%d_%H:%M:%S}'.format(datetime.now()))
        print('best epoch: {:d}'.format(epoch_best))
        print('checkpoint: ', save_path_best)
                
        return uer_best, epoch_best, save_path 
    
    def train_profile(self,
              num_epochs=400,
              try_restore_latest_checkpoint=False
              ):
        with tf.contrib.tfprof.ProfileContext('./tf_prof') as pctx:
            self.train(num_epochs, try_restore_latest_checkpoint)

    def evaluate(self, checkpoint_path, epoch=None, alignments_outdir='./alignments/tmp/'):
        self._evaluate_model.model.saver.restore(
            sess=self._evaluate_session,
            save_path=checkpoint_path
        )
        self._evaluate_session.run([stream.iterator_initializer for stream in self._evaluate_model.data
                                     if stream is not None])
        predictions_dict = {}
        labels_dict = {}

        model = self._evaluate_model
        data = model.data[0] if model.data[0] is not None else model.data[1]
        session_outputs = [self._evaluate_model.model._decoder.inference_predicted_ids,
                           data.labels,
                           data.inputs_filenames,
                           data.labels_filenames,]

        if self._write_attention_alignment is True:
            session_outputs.append(self._evaluate_model.model.attention_summary)

        while True:

            try:
                out = self._evaluate_session.run(session_outputs)

                # debug time
                #assert (any(list(out[2] == out[3])))

                if self._write_attention_alignment is True:
                    imag_summ = tf.Summary()
                    imag_summ.ParseFromString(out[-1])

                for idx in range(len(out[2])):  # could use batch_size here, but take care with the last smaller batch
                    predicted_ids = out[0][idx]
                    predicted_symbs = [self._unit_dict[sym] for sym in predicted_ids]

                    labels_ids = out[1][idx]
                    labels_symbs = [self._unit_dict[sym] for sym in labels_ids]

                    file = out[2][idx].decode('utf-8')

                    if self._write_attention_alignment is True:
                        makedirs(alignments_outdir, exist_ok=True)
                        with tf.gfile.GFile(path.join(alignments_outdir, file + '.png'), mode='w') as img_f:
                            img_f.write(imag_summ.value[idx].image.encoded_image_string)

                    predictions_dict[file] = predicted_symbs
                    labels_dict[file] = labels_symbs

            except tf.errors.OutOfRangeError:
                break

        uer = compute_wer(predictions_dict, labels_dict)
        error_rate = {self._unit: uer}
        if self._unit == 'character':
            wer = compute_wer(predictions_dict, labels_dict, split_words=True)
            error_rate['word'] = wer
            
        for (k,v) in error_rate.items():
            message = '{:4s}: {:.4f}%\n'.format(k, v * 100) 
            self._log_fd.write(message)
            self._log_fd.flush()
            print(message)

        outdir = path.join('predictions', path.basename(path.dirname(checkpoint_path)))
        makedirs(outdir, exist_ok=True)
        write_sequences_to_labelfile(predictions_dict, path.join(outdir, 'predicted_epoch_{}.mlf'.format(epoch)))

        return error_rate

    def _create_graphs(self):
        self._train_graph = tf.Graph()
        self._evaluate_graph = tf.Graph()
        self._predict_graph = tf.Graph()

    def _create_models(self):
        self._train_model = self._make_model(
            graph=self._train_graph,
            mode='train',
            batch_size=self._hparams.batch_size[0])
        self._evaluate_model = self._make_model(
            graph=self._evaluate_graph,
            mode='evaluate',
            batch_size=self._hparams.batch_size[1])

    def _create_sessions(self):
        config = tf.ConfigProto(allow_soft_placement=True) ##, log_device_placement=True)
        self._train_session = tf.Session(graph=self._train_graph, config=config)
        self._evaluate_session = tf.Session(graph=self._evaluate_graph, config=config)
        self._predict_session = tf.Session(graph=self._predict_graph, config=config)

    def _initialize_sessions(self):
        run_options = tf.RunOptions(report_tensor_allocations_upon_oom=True)
        self._train_session.run(self._train_model.initializer, options=run_options)
        self._evaluate_session.run(self._evaluate_model.initializer)

    def _make_model(self, graph, mode, batch_size):
        with graph.as_default():

            video_data, audio_data = self._fetch_data(mode, batch_size)
            video_features, audio_features = self._preprocess_data(video_data, audio_data, mode, batch_size)

            model = Seq2SeqModel(
                data_sequences=(video_features, audio_features),
                mode=mode,
                hparams=self._hparams
            )

            initializer = tf.global_variables_initializer()

            # Returning the original data, not the processed features
            return Model(data=(video_data, audio_data),
                         model=model,
                         initializer=initializer,
                         batch_size=batch_size)

    def _initialize_log(self, logfile):
        time = datetime.now()
        if logfile is None:
            name = "{:%Y-%m-%d_%H:%M:%S}".format(time)
            logfile = path.join('logs', name)
        makedirs(path.dirname(logfile), exist_ok=True)
        
        try:
            self._log_fd = open(logfile, 'a')
        except:
            raise
        
        header = '{:=^40}\n'.format(" {:%Y-%m-%d %H:%M:%S} ".format(time))
        self._log_fd.write(header)
        self._log_fd.write("Parameters:")
        pprint(vars(self), self._log_fd)
        self._log_fd.write("Hyperparameters:")
        # get all vars in _hparams without '_hparam_types'
        hp = vars(self._hparams).copy()
        del hp['_hparam_types']
        pprint(hp, self._log_fd)
        self._log_fd.write("Audio Hyperparameters:")
        # get all vars in _hparams without '_hparam_types'
        hp = vars(self._hparams_audio).copy()
        del hp['_hparam_types']
        pprint(hp, self._log_fd)
        self._log_fd.flush()

    def _parse_iterator(self, iterator):
        inputs = tf.cast(iterator.inputs, dtype=self._hparams.dtype)
        labels = tf.cast(iterator.labels, tf.int32, name='labels')
        inputs_length = tf.cast(iterator.inputs_len, tf.int32, name='inputs_len')
        labels_length = tf.cast(iterator.labels_len, tf.int32, name='labels_len')

        return Data(
            inputs=inputs,
            inputs_length=inputs_length,
            inputs_filenames=iterator.filename,
            labels=labels,
            labels_length=labels_length,
            labels_filenames=iterator.filename2,
            iterator_initializer=iterator.initializer)

    def _parse_multimodal_iterator(self, iterator):
        vid_inputs = tf.cast(iterator.inputs[0], dtype=self._hparams.dtype, name='vid_inputs')
        vid_inputs_length = tf.cast(iterator.inputs_len[0], tf.int32, name='vid_inputs_len')
        aud_inputs = tf.cast(iterator.inputs[1], dtype=self._hparams.dtype, name='aud_inputs')
        aud_inputs_length = tf.cast(iterator.inputs_len[1], tf.int32, name='aud_inputs_len')

        labels = tf.cast(iterator.labels, tf.int32, name='labels')
        labels_length = tf.cast(iterator.labels_len, tf.int32, name='labels_len')

        video_data = Data(
            inputs=vid_inputs,
            inputs_length=vid_inputs_length,
            inputs_filenames=iterator.filename[0],
            labels=labels,
            labels_length=labels_length,
            labels_filenames=iterator.filename2,
            iterator_initializer=iterator.initializer)

        audio_data = Data(
            inputs=aud_inputs,
            inputs_length=aud_inputs_length,
            inputs_filenames=iterator.filename[1],
            labels=labels,
            labels_length=labels_length,
            labels_filenames=iterator.filename2,
            iterator_initializer=iterator.initializer)

        return video_data, audio_data

    def _fetch_data(self, mode, batch_size):

        video_data = None
        audio_data = None

        if self._video_processing is not None and self._audio_processing is not None:

            iterator = make_iterator_from_two_records(
                video_record=self._video_train_record if mode == 'train' else self._video_test_record,
                audio_record=self._audio_train_record if mode == 'train' else self._audio_test_record,
                label_record=self._labels_train_record if mode == 'train' else self._labels_test_record,
                batch_size=batch_size,
                unit_dict=self._hparams.unit_dict,
                shuffle=True if mode == 'train' else False,
                reverse_input=False,
                bucket_width=15,  # 0.5sec at 30 fps,
                suppress=self._suppress_mode
            )
            video_data, audio_data = self._parse_multimodal_iterator(iterator)

        else:
            if self._video_processing is not None:
                video_iterator = make_iterator_from_one_record(
                    data_record=self._video_train_record if mode == 'train' else self._video_test_record,
                    label_record=self._labels_train_record if mode == 'train' else self._labels_test_record,
                    batch_size=batch_size,
                    unit_dict=self._hparams.unit_dict,                    
                    shuffle=True if mode == 'train' else False,
                    reverse_input=False,
                    bucket_width=15,  # 0.5sec at 30 fps
                )

                video_data = self._parse_iterator(video_iterator)

            if self._audio_processing is not None:
                audio_iterator = make_iterator_from_one_record(
                    data_record=self._audio_train_record if mode == 'train' else self._audio_test_record,
                    label_record=self._labels_train_record if mode == 'train' else self._labels_test_record,
                    batch_size=batch_size,
                    unit_dict=self._hparams.unit_dict,
                    shuffle=True if mode == 'train' else False,
                    reverse_input=False,
                    bucket_width=50,  # 0.5 sec at 100 mfcc/sec
                )

                audio_data = self._parse_iterator(audio_iterator)

        return video_data, audio_data

    def _preprocess_data(self, video_data, audio_data, mode, batch_size):

        if self._video_processing is not None:

            if 'cnn' in self._video_processing:
                visual_features = cnn_layers(
                    inputs=video_data.inputs,
                    cnn_type=self._video_processing,
                    is_training=(mode=='train'),
                    cnn_filters=self._hparams_video.cnn_filters,
                    cnn_dense_units=self._hparams_video.cnn_dense_units
                )

                # re-create video_data to update the `inputs` field
                video_data = Data(
                    inputs=visual_features,
                    inputs_length=video_data.inputs_length,
                    inputs_filenames=video_data.inputs_filenames,
                    labels=video_data.labels,
                    labels_length=video_data.labels_length,
                    labels_filenames=video_data.labels_filenames,
                    iterator_initializer=video_data.iterator_initializer
                )

            elif self._video_processing == 'features':
                pass
            else:
                raise Exception('unknown visual content')
        else:
            pass

        if self._audio_processing is not None:

            if self._audio_processing == 'wav':  # compute mfcc on the fly

                audio_features = process_audio(
                    audio_data.inputs,
                    hparams=self._hparams_audio,
                )

                # re-create audio_data to update the `inputs` field
                audio_data = Data(
                    inputs=audio_features,
                    inputs_length=audio_data.inputs_length,
                    inputs_filenames=audio_data.inputs_filenames,
                    labels=audio_data.labels,
                    labels_length=audio_data.labels_length,
                    labels_filenames=audio_data.labels_filenames,
                    iterator_initializer=audio_data.iterator_initializer
                )

            elif self._audio_processing == 'features':
                pass
            else:
                raise Exception('unknown audio content ',self._audio_processing)
        else:
            pass

        return video_data, audio_data
