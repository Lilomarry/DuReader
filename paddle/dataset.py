# -*- coding:utf8 -*-
###############################################################################
#
# Copyright (c) 2017 Baidu.com, Inc. All Rights Reserved
#
###############################################################################
"""
Implements data parsers for different tasks on DuReader dataset.

Authors: liuyuan(liuyuan04@baidu.com)
Data: 2017/09/20 12:00:00
"""
import copy
import hashlib
import itertools
import logging
import json
import numpy as np
import random
import sys
from collections import OrderedDict
import paddle.v2 as paddle
from find_answer import find_best_query_match

logger = logging.getLogger("paddle")
logger.setLevel(logging.INFO)


class Dataset(object):
    """
    Base dataset class for various tasks.
    """
    def __init__(self,
                 file_name=None,
                 vocab_file=None,
                 vocab_size=0,
                 shuffle=False,
                 selected=-1,
                 preload=True,
                 append_raw=False,
                 is_infer=False,
                 max_p_len=500):
        self.file_name = file_name
        self.data = []
        self.raw = []
        self.vocab = self.read_vocab(vocab_file, vocab_size) \
                        if vocab_file else {}
        self.shuffle = shuffle
        self.selected = selected
        self.unk_id = 0
        self.preload = preload
        self.max_p_len = max_p_len
        self.append_raw = append_raw
        self.is_infer = is_infer
        self.doc_num = 5
        if preload:
            logger.info('Preloading data...')
            self.load()
            logger.info('Done, data[{}]'.format(len(self.data)))

    def load(self):
        """
        Loads all data records into self.data.
        """
        self.data = []
        with open(self.file_name, 'r') as src:
            for line in src:
                self.data += self.parse(line.strip())
        if self.shuffle:
            logger.info('Shuffling data...')
            random.shuffle(self.data)
        if self.selected > 0:
            self.data = self.data[:self.selected]

    def read_vocab(self, vocab_file, vocab_size):
        """
        Builds vocabulary dictionary.

        Args:
            vocab_file: file name of the vocabulary file, vocabulary file
                        should contain 2 columns separated by tab. The 1st
                        column is token, the 2nd is the count of the token
                        in the dataset.
            vocab_size: An integer indicates size of the vocabulary dictionary,
                        the size includes UNK.
        Returns:
            A dictionary mapping a token to a index. The size of the returned
            dictionary is vocab_size - 1, because UNK is not in the dict,
            the index of UNK is 0, if a token is not in this dict, it will be
            assigned to index 0. Tokens in this dict are indexed from 1.
        """
        vocab = {}
        with open(vocab_file, 'r') as vf:
            ln_cnt = 1
            for line in vf:
                if vocab_size > 0 and ln_cnt > vocab_size - 1:
                    break
                line = unicode(line, encoding='utf8')
                w, c = line.split('\t')
                vocab[w] = ln_cnt
                ln_cnt += 1

        # unk is not in vocab dict. but is counted in the vocab_size
        assert len(vocab) == vocab_size - 1, \
                "{} vs {}".format(len(vocab), vocab_size)
        logger.info('vocab size: {}'.format(len(vocab) + 1))
        return vocab

    def parse(self, line):
        """
        Implements parser for specific task, parses one line and returns a
        record as described by self.schema.
        """
        raise NotImplementedError

    def create_reader(self):
        """
        Creates reader generator.

        Returns:
            A generator, which yields one data record once.
        """
        def _reader_preload():
            if self.shuffle:
                logger.info('shuffling data ...')
                random.shuffle(self.data)
            for line in self.data:
                if not line:
                    logger.info("skip empty line: {}".format(line))
                    continue
                yield line

        def _reader_stream():
            with open(self.file_name, 'r') as fn:
                for line in fn:
                    data = self.parse(line.strip())
                    if not data:
                        continue
                    for d in data:
                        yield d

        if not self.preload:
            return _reader_stream
        return _reader_preload


class DuReaderYesNo(Dataset):
    """
    Implements parser for yesno task.
    """
    def __init__(self, *args, **kwargs):
        super(DuReaderYesNo, self).__init__(*args, **kwargs)
        self.schema = ['q_ids', 'a_ids', 'label']
        self.feeding = {name: i for i, name in enumerate(self.schema)}

    def parse(self, line):
        """
        Parses one line.

        Args:
            line: A legal json string.

        Returns:
            A record as self.schema describes.
        """
        obj = json.loads(line.strip())
        label = obj['label']
        query = [
                self.vocab.get(x, self.unk_id)
                for x in obj['segmented_query']]
        para = [
                self.vocab.get(x, self.unk_id)
                for x in obj['segmented_answer']]

        ret = []
        if not query or not para or label not in set(range(4)):
            return ret
        record = [query, para, label]
        if self.is_infer:
            record.append(obj)
        ret.append(record)
        return ret


class DuReaderQA(Dataset):
    """
    Implements parser for QA task.
    """
    def __init__(self, *args, **kwargs):
        super(DuReaderQA, self).__init__(*args, **kwargs)

        doc_names = ['doc' + str(i) for i in range(self.doc_num)]
        start_label_names = ['start_pos' + str(i) for i in range(self.doc_num)]
        end_label_names = ['end_pos' + str(i) for i in range(self.doc_num)]
        doc_len_names = ['len' + str(i) for i in range(self.doc_num)]

        self.schema = ['q_ids'] \
                      + doc_names \
                      + doc_len_names \
                      + start_label_names \
                      + end_label_names

        self.feeding = {name: i for i, name in enumerate(self.schema)}

    def __find_ans_span(self, query_tokens, span, answer_docs, docs):
        assert len(span) == 1, 'Multiple spans: {}'.format(span)
        assert len(answer_docs) == 1, \
                'Multiple answer docs: {}'.format(answer_docs)
        selected_paras = []
        answer_docs = set(answer_docs)
        para_tokens = []
        for i, doc in enumerate(docs):
            if not self.is_infer:
                para_idx = doc['most_related_paragraph']
            else:
                para_idx = find_best_query_match(doc, query_tokens)
            para = doc['segmented_paragraphs'][para_idx]
            if len(para) == 0:
                continue
            ans_span = (-1, -1)
            if i in answer_docs:
                ans_span = span[0]
            if len(para) > self.max_p_len:
                para = para[:self.max_p_len]
            s, e = ans_span
            if s >= len(para):
                logger.info('Skip span out of para length.')
                continue
            e = min(len(para) - 1, e)
            ans_span = (s, e)
            para_ids = [self.vocab.get(x, self.unk_id) for x in para]
            selected_paras.append((para_ids, ans_span))
            para_tokens.append(para)
        return selected_paras, para_tokens

    def __make_sample(self, query_ids, para_infos):
        def __get_label(idx, ref):
            ret = [0.0] * len(ref)
            if idx > 0:
                ret[idx] = 1.0
            return [[x] for x in ret]

        paras, start_labels, end_labels, para_lens = [], [], [], []
        default_para_info = ([0], (-1, -1))
        selected = para_infos[:self.doc_num]
        if len(selected) < self.doc_num:
            selected += [default_para_info] * (self.doc_num - len(selected))
        for para_ids, ans_span in selected:
            s, e = ans_span
            start_label = __get_label(s, para_ids)
            end_label = __get_label(e, para_ids)
            paras.append(para_ids)
            start_labels.append(start_label)
            end_labels.append(end_label)
            para_lens.append([[len(para_ids)]])
        sample = [query_ids] + paras + start_labels + end_labels + para_lens
        return sample

    def __get_infer_info(self, obj, paras):
        info = {}
        info['tokens'] = list(itertools.chain(*paras))
        info['answer'] = obj['answers']
        info['question'] = obj['query']
        return info

    def parse(self, line):
        """
        Parses one line.

        Args:
            line: A legal json string.

        Returns:
            A record as self.schema describes.
        """
        ret = []
        obj = json.loads(line)
        if len(obj['answer_docs']) != 1:
            logger.info('skip, wrong answer docs')
            return ret
        if obj['answer_docs'][0] > 5:
            logger.info('skip, answer doc out of range.')
            return ret
        q_ids = [self.vocab.get(x, self.unk_id) for x in obj['segmented_query']]
        if len(q_ids) == 0:
            return ret
        selected_paras, para_tokens = self.__find_ans_span(
                obj['segmented_query'],
                obj['answer_spans'],
                obj['answer_docs'],
                obj['documents'])
        if not selected_paras:
            return ret
        sample = self.__make_sample(q_ids, selected_paras)
        assert len(sample) == len(self.schema)
        if self.is_infer:
            sample.append(self.__get_infer_info(obj, para_tokens))
        ret.append(sample)
        return ret


class DuReaderRanking(Dataset):
    """
    Implements parser for ranking task.
    """
    def __init__(self, pairwise=True, *args, **kwargs):
        self.pairwise = pairwise
        self.feeding = {'q_ids': 0,
                       'p_ids_left': 1,
                       'p_ids_right': 2,
                       'label': 3,
                       'label_int': 4}
        schema = [(k, v) for k, v in self.feeding.items()]
        self.schema = [x[0] for x in sorted(schema, key=lambda x: x[1])]
        self.pool = []
        self.pool_max = 10000
        super(BaiduNlpRanking, self).__init__(*args, **kwargs)

    def parse(self, line):
        """
        Parses one line.

        Args:
            line: A legal json string.

        Returns:
            A record as self.schema describes.
        """
        ret = []
        obj = json.loads(line)
        q_ids = [self.vocab.get(x, self.unk_id) \
                 for x in obj['segmented_query']]
        passages = [obj['documents'][i]['segmented_paragraphs'] \
                    for i in obj['answer_docs']]
        para_ids = [obj['documents'][i]['most_related_paragraph'] \
                    for i in obj['answer_docs']]
        pos_paras = [p[i] for p, i in zip(passages, para_ids)]

        if not pos_paras:
            return ret

        if len(self.pool) >= len(pos_paras):
            neg_paras = random.sample(self.pool, len(pos_paras))
            for p, n in zip(pos_paras, neg_paras):
                pids = [self.vocab.get(x, self.unk_id) for x in p]
                nids = [self.vocab.get(x, self.unk_id) for x in n]
                if random.random < 0.5:
                    ret.append((q_ids, pids, nids, [[1.0]], 1))
                else:
                    ret.append((q_ids, nids, pids, [[0.0]], 0))

        neg_docs_hard = []
        for p, i in zip(passages, para_ids):
            for j, para in enumerate(p):
                if j != i:
                    neg_docs_hard.append(para)

        if len(self.pool) > self.pool_max:
            random.shuffle(self.pool)
            self.pool = self.pool[:self.pool_max / 2]

        self.pool += neg_docs_hard
        for i, doc in enumerate(obj['documents']):
            if i in set(obj['answer_docs']):
                continue
            self.pool += doc['segmented_paragraphs']
        return ret


if __name__ == '__main__':
    data = sys.argv[1]
    vocab = sys.argv[2]
    baidu = DuReaderQA(file_name=data,
            vocab_file=vocab,
            preload=False,
            max_p_len=300,
            is_infer=True,
            append_raw=False,
            vocab_size=218967)

    # test reader
    reader = baidu.create_reader()
    for r in reader():
        print r

