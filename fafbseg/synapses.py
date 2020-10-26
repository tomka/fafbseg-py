# A collection of tools to interface with manually traced and autosegmented data
# in FAFB.
#
#    Copyright (C) 2019 Philipp Schlegel
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.

import navis
import os
import sqlite3

import networkx as nx
import numpy as np
import pandas as pd

from scipy.spatial import cKDTree
from tqdm.auto import tqdm

from . import search

conn = None


def get_connection(filepath=None, force_reconnect=False):
    """Connect to SQL DB containg the synapses."""
    global conn

    # This prevents us from intializing many connections
    if conn and not force_reconnect:
        if filepath:
            print('A connection to a database already exists. Call '
                  'get_connection() with `force_reconnect=True` to '
                  'force re-initialization.')
        return conn

    if not filepath:
        filepath = os.environ.get('BUHMANN_SYNAPSE_DB', None)

    if not filepath:
        raise ValueError('Must provided filepath to SQL synapse database '
                         'either as `filepath` parameter or as '
                         '`BUHMANN_SYNAPSE_DB` environment variable.')

    conn = sqlite3.connect(filepath)

    return conn


def get_segment_synapses(seg_ids, pre=True, post=True, score_thresh=30,
                         ret='brief', db=None):
    """Fetch synapses for given segment IDs.

    Parameters
    ----------
    seg_ids :       int | list of int
                    Segment IDs to fetch synapses for.
    pre/post :      bool
                    Whether to fetch pre- and/or postsynapses.
    score_thresh :  int, optional
                    If provided will only return synapsed with a cleft with this
                    or higher. The default of 30 is used in the original Buhmann
                    paper.
    ret :           "brief" | "full"
                    If "full" will return all fields. If "brief" will omit some
                    of the less relevant fields.
    db :            str, optional
                    Must point to SQL database containing the synapse data. If
                    not provided will look for a `BUHMANN_SYNAPSE_DB`
                    environment variable.

    Return
    ------
    pd.DataFrame

    """
    assert ret in ('brief', 'full')
    assert isinstance(score_thresh, (type(None), int, float))

    conn = get_connection(db)

    seg_ids = navis.utils.make_iterable(seg_ids)

    # Turn into query string
    seg_ids_str = f'{",".join(seg_ids.astype(str))}'

    # Create query
    if ret == 'brief':
        cols = ['pre_x', 'pre_y', 'pre_z', 'post_x', 'post_y', 'post_z', 'scores',
                'cleft_id', 'cleft_scores', 'segmentid_post', 'segmentid_pre']
    else:
        cols = ['*']
    sel = f'SELECT {", ".join(cols)} from synlinks'

    if pre and post:
        where = f'WHERE (segmentid_pre IN ({seg_ids_str}) OR segmentid_post in ({seg_ids_str}))'
    elif pre:
        where = f'WHERE segmentid_pre IN ({seg_ids_str})'
    elif post:
        where = f'WHERE segmentid_post IN ({seg_ids_str})'
    else:
        raise ValueError('`pre` and `post` must not both be False')

    if score_thresh:
        where += f' AND cleft_scores >= {score_thresh}'

    return pd.read_sql(f'{sel} {where};', conn)


def get_neuron_synapses(x, pre=True, post=True, score_thresh=30, ol_thresh=5,
                        dist_thresh=2000, attach=True, drop_duplicates=True,
                        db=None, verbose=True, progress=True):
    """Fetch synapses for a given neuron.

    Works by:
     1. Fetch segment IDs corresponding to a neuron
     2. Fetch Buhmann et al. synapses associated with these segment IDs
     3. Do some clean-up. See parameters for details - defaults are those used
        by Buhmann et al

    Notes
    -----
    1. The ``connector_id`` columns is a simple enumeration and is effectively
       meaningless.

    Parameters
    ----------
    x :             navis.Neuron | pymaid.CatmaidNeuron | NeuronList
                    Neurons to fetch synapses for.
    pre/post :      bool
                    Whether to fetch pre- and/or postsynapses.
    ol_thresh :     int, optional
                    If provided, will required a minimum node overlap between
                    a neuron and a segment for that segment to be included
                    (see step 1 above).
    score_thresh :  int, optional
                    If provided will only return synapses with a cleft score of
                    this or higher.
    dist_thresh :   int
                    Synapses further away than the given distance [nm] from any
                    node in the neuron than given distance will be discarded.
    attach :        bool
                    If True, will attach synapses as `.connectors` to neurons.
                    If False, will return DataFrame with synapses.
    drop_duplicates :   bool
                        If True, will merge synapses which connect the same
                        pair of pre-post segmentation IDs and are within
                        less than 250nm.
    db :            str, optional
                    Must point to SQL database containing the synapse data. If
                    not provided will look for a `BUHMANN_SYNAPSE_DB`
                    environment variable.
    progress :      bool
                    Whether to show progress bars or not.

    Return
    ------
    pd.DataFrame
                    Only if ``attach=False``.

    """
    # This is just so we throw a DB exception early and not wait for fetching
    # segment Ids first
    _ = get_connection(db)

    assert isinstance(x, (navis.BaseNeuron, navis.NeuronList))

    if not isinstance(x, navis.NeuronList):
        x = navis.NeuronList([x])

    # Get segments for this neuron(s)
    seg_ids = search.neuron_to_segments(x)

    # Drop segments with overlap below threshold
    if ol_thresh:
        seg_ids = seg_ids.loc[seg_ids.max(axis=1) >= ol_thresh]

    # Fetch pre- and postsynapses associated with these segments
    # It's cheaper to get them all in one go
    syn = get_segment_synapses(seg_ids.index.values,
                               score_thresh=score_thresh,
                               db=None)

    # Now associate synapses with neurons
    tables = []
    for c in tqdm(seg_ids.columns,
                  desc='Proc. neurons',
                  disable=not progress,
                  leave=False):
        this_seg = seg_ids.loc[seg_ids[c].notnull(), c].index.values
        this_pre = syn[syn.segmentid_pre.isin(this_seg)]
        this_post = syn[syn.segmentid_post.isin(this_seg)]

        # Drop connections that connect the same two segmentation IDs and are
        # within a distance of 250nm
        dupl_thresh = 250
        if drop_duplicates:
            # Deal with pre- and postsynapses separately
            while True:
                pre_tree = cKDTree(this_pre[['pre_x', 'pre_y', 'pre_z']].values)
                pairs = pre_tree.query_pairs(r=dupl_thresh, output_type='ndarray')

                same_pre = this_pre.iloc[pairs[:, 0]].segmentid_pre.values == this_pre.iloc[pairs[:, 1]].segmentid_pre.values
                same_post = this_pre.iloc[pairs[:, 0]].segmentid_post.values == this_pre.iloc[pairs[:, 1]].segmentid_post.values
                same_cn = same_pre & same_post

                # If no more pairs to collapse break
                if same_cn.sum() == 0:
                    break

                # Generate a graph from pairs
                G = nx.Graph()
                G.add_edges_from(pairs[same_cn])

                # Find the minimum number of nodes we need to remove
                # to separate the connectors
                to_rm = []
                for cn in nx.connected_components(G):
                    to_rm += list(nx.minimum_node_cut(nx.subgraph(G, cn)))
                this_pre = this_pre.drop(index=this_pre.index.values[to_rm])

            while True:
                post_tree = cKDTree(this_post[['post_x', 'post_y', 'post_z']].values)
                pairs = post_tree.query_pairs(r=dupl_thresh, output_type='ndarray')

                same_pre = this_post.iloc[pairs[:, 0]].segmentid_pre.values == this_post.iloc[pairs[:, 1]].segmentid_pre.values
                same_post = this_post.iloc[pairs[:, 0]].segmentid_post.values == this_post.iloc[pairs[:, 1]].segmentid_post.values
                same_cn = same_pre & same_post

                # If no more pairs to collapse break
                if same_cn.sum() == 0:
                    break

                # Generate a graph from pairs
                G = nx.Graph()
                G.add_edges_from(pairs[same_cn])

                # Find the minimum number of nodes we need to remove
                # to separate the connectors
                to_rm = []
                for cn in nx.connected_components(G):
                    to_rm += list(nx.minimum_node_cut(nx.subgraph(G, cn)))
                this_post = this_post.drop(index=this_post.index.values[to_rm])

        # Combine pre and postsynapses
        connectors = np.vstack((this_pre[['pre_x', 'pre_y', 'pre_z', 'cleft_scores']].values,
                                this_post[['post_x', 'post_y', 'post_z', 'cleft_scores']].values))
        connectors = pd.DataFrame(connectors, columns=['x', 'y', 'z', 'cleft_scores'])
        connectors['type'] = 'post'
        connectors.loc[:this_pre.shape[0], 'type'] = 'pre'

        # Map connectors to nodes
        neuron = x.idx[c]
        tree = navis.neuron2KDTree(neuron)
        dist, ix = tree.query(connectors[['x', 'y', 'z']].values,
                              distance_upper_bound=dist_thresh)

        # Drop far away connectors
        connectors = connectors.loc[dist < np.inf]

        # Assign node IDs
        connectors['node_id'] = neuron.nodes.iloc[ix[dist < np.inf]].node_id.values

        # Make fake IDs
        connectors['connector_id'] = np.arange(connectors.shape[0])

        if attach:
            neuron.connectors = connectors.reset_index(drop=True)
        else:
            connectors['neuron'] = neuron.id
            tables.append(connectors)

    if not attach:
        return pd.concat(tables, axis=0, sort=True).reset_index(drop=True)
