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

import itertools
import networkx as nx
import numpy as np
import pandas as pd
import pymaid

from tqdm import tqdm

from .search import find_fragments, find_autoseg_fragments

import inquirer
from inquirer.themes import GreenPassion

from . import utils
use_pbars = utils.use_pbars

# This is to prevent FutureWarning from numpy (via vispy)
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)


@utils.never_cache
def find_missed_branches(x, autoseg_instance, tag=True, tag_size_thresh=10,
                         min_node_overlap=4, **kwargs):
    """Use autoseg to find (and annotate) potential missed branches.

    Parameters
    ----------
    x :                 pymaid.CatmaidNeuron/List
                        Neuron(s) to search for missed branches.
    autoseg_instance :  pymaid.CatmaidInstance
                        CATMAID instance containing the autoseg skeletons.
    tag :               bool, optional
                        If True, will tag nodes of x that might have missed
                        branches with "missed branch?".
    tag_size_thresh :   int, optional
                        Size threshold in microns of cable for tagging
                        potentially missed branches.
    min_node_overlap :  int, optional
                        Minimum number of nodes that input neuron(s) x must
                        overlap with given segmentation ID for it to be
                        included.
    **kwargs
                        Keyword arguments passed to
                        ``fafbseg.neuron_from_segments``.

    Returns
    -------
    summary :           pandas.DataFrame
                        DataFrame containing a summary of potentially missed
                        branches.

                        If input is a single neuron:

    fragments :         pymaid.CatmaidNeuronList
                        Fragments found to be potentially overlapping with the
                        input neuron.
    branches :          pymaid.CatmaidNeuronList
                        Potentially missed branches extracted from ``fragments``.

    Examples
    --------
    Setup

    >>> import fafbseg
    >>> import pymaid

    >>> # Set up connections to manual and autoseg CATMAID
    >>> manual = pymaid.CatmaidInstance('URL', 'HTTP_USER', 'HTTP_PW', 'API_TOKEN')
    >>> auto = pymaid.CatmaidInstance('URL', 'HTTP_USER', 'HTTP_PW', 'API_TOKEN')

    >>> # Set a source for segmentation data
    >>> fafbseg.use_google_storage("https://storage.googleapis.com/fafb-ffn1-20190805/segmentation")

    Find missed branches and tag them

    >>> # Fetch a neuron
    >>> x = pymaid.get_neuron(16, remote_instance=manual)
    >>> # Find and tag missed branches
    >>> (summary,
    ...  fragments,
    ...  branches) = fafbseg.find_missed_branches(x, autoseg_instance=auto)

    >>> # Show summary of missed branches
    >>> summary.head()
       n_nodes  cable_length   node_id
    0      110     28.297424   3306395
    1       90     23.976504  20676047
    2       64     15.851333  23419997
    3       29      7.494350   6298769
    4       16      3.509739  15307841

    >>> # Co-visualize your neuron and potentially overlapping autoseg fragments
    >>> x.plot3d(color='w')
    >>> fragments.plot3d()

    >>> # Visualize the potentially missed branches
    >>> pymaid.clear3d()
    >>> x.plot3d(color='w')
    >>> branches.plot3d(color='r')

    """
    if isinstance(x, pymaid.CatmaidNeuronList):
        to_concat = []
        for n in tqdm(x, desc='Processing neurons', disable=not use_pbars, leave=False):
            (summary,
             frags,
             branches) = find_missed_branches(n,
                                              autoseg_instance=autoseg_instance,
                                              tag=tag,
                                              tag_size_thresh=tag_size_thresh,
                                              **kwargs)
            summary['skeleton_id'] = n.skeleton_id
            to_concat.append(summary)

        return pd.concat(to_concat, ignore_index=True)
    elif not isinstance(x, pymaid.CatmaidNeuron):
        raise TypeError('Input must be CatmaidNeuron/List, got "{}"'.format(type(x)))

    # Find autoseg neurons overlapping with input neuron
    nl = find_autoseg_fragments(x,
                                autoseg_instance=autoseg_instance,
                                min_node_overlap=min_node_overlap,
                                verbose=False,
                                raise_none_found=False)

    # Next create a union
    if not nl.empty:
        for n in nl:
            n.nodes['origin'] = 'autoseg'
            n.nodes['origin_skid'] = n.skeleton_id
        x.nodes['origin'] = 'query'
        x.nodes['origin_skid'] = x.skeleton_id
        union = pymaid.union_neurons(x, nl, base_neuron=x, limit=2, non_overlap='stitch')

        # Subset to autoseg nodes
        autoseg_nodes = union.nodes[union.nodes.origin == 'autoseg'].treenode_id.values
    else:
        autoseg_nodes = np.empty((0, 5))

    # Process fragments if any autoseg nodes left
    data = []
    frags = pymaid.CatmaidNeuronList([])
    if autoseg_nodes.shape[0]:
        autoseg = pymaid.subset_neuron(union, autoseg_nodes)

        # Split into fragments
        frags = pymaid.break_fragments(autoseg)

        # Generate summary
        nodes = union.nodes.set_index('treenode_id')
        for n in frags:
            # Find parent node in union
            pn = nodes.loc[n.root[0], 'parent_id']
            pn_co = nodes.loc[pn, ['x', 'y', 'z']].values
            org_skids = n.nodes.origin_skid.unique().tolist()
            data.append([n.n_nodes, n.cable_length, pn, pn_co, org_skids])

    df = pd.DataFrame(data, columns=['n_nodes', 'cable_length', 'node_id',
                                     'node_loc', 'autoseg_skids'])
    df.sort_values('cable_length', ascending=False, inplace=True)

    if tag and not df.empty:
        to_tag = df[df.cable_length >= tag_size_thresh].node_id.values

        resp = pymaid.add_tags(to_tag,
                               tags='missed branch?',
                               node_type='TREENODE',
                               remote_instance=x._remote_instance)

        if 'error' in resp:
            return df, resp

    return df, nl, frags


@utils.never_cache
def merge_neuron(x, target_instance, tag, min_node_overlap=4, min_overlap_size=1,
                 merge_limit=1, min_upload_size=0, min_upload_nodes=1,
                 update_radii=True, import_tags=False, label_joins=True,
                 sid_from_nodes=True):
    """Merge neuron into target instance.

    This function will attempt to:

        1. Find fragments in ``target_instance`` that overlap with ``x``
           using whatever segmentation data source you have set using
           ``fafbseg.use_...``.
        2. Generate a union of these fragments and ``x``.
        3. Make a differential upload of the union leaving existing nodes
           untouched.
        4. Join uploaded and existing tracings into a single continuous
           neuron. This will also upload connectors but no node tags.

    Parameters
    ----------
    x :                 pymaid.CatmaidNeuron/List
                        Neuron(s)/fragment(s) to commit to ``target_instance``.
    target_instance :   pymaid.CatmaidInstance
                        Target Catmaid instance to commit the neuron to.
    tag :               str
                        A tag to be added as part of a ``{URL} upload {tag}``
                        annotation. This should be something identifying your
                        group - e.g. ``tag='WTCam'`` for the Cambridge Wellcome
                        Trust group.
    min_node_overlap :  int, optional
                        Minimal overlap between `x` and a potentially
                        overlapping neuron in ``target_instance``. If
                        the fragment has less total nodes than `min_overlap`,
                        the threshold will be lowered to:
                        ``min_overlap = min(min_overlap, fragment.n_nodes)``
    min_overlap_size :  int, optional
                        Minimum node count for potentially overlapping neurons
                        in ``target_instance``. Use this to e.g. exclude
                        single-node synapse orphans.
    merge_limit :       int, optional
                        Distance threshold [um] for collapsing nodes of ``x``
                        into overlapping fragments in target instance. Decreasing
                        this will help if your neuron has complicated branching
                        patterns (e.g. uPN dendrites) at the cost of potentially
                        creating duplicate parallel tracings in the neuron's
                        backbone.
    min_upload_size :   float, optional
                        Minimum size in microns for upload of new branches:
                        branches found in ``x`` but not in the overlapping
                        neuron(s) in ``target_instance`` are uploaded in
                        fragments. Use this parameter to exclude small branches
                        that might not be worth the additional review time.
    min_upload_nodes :  int, optional
                        As ``min_upload_size`` but for number of nodes instead
                        of cable length.
    update_radii :      bool, optional
                        If True, will use radii in ``x`` to update radii of
                        overlapping fragments if (and only if) the nodes
                        do not currently have a radius (i.e. radius<=0).
    import_tags :       bool, optional
                        If True, will import node tags. Please note that this
                        will NOT import tags of nodes that have been collapsed
                        into manual tracings.
    label_joins :       bool, optional
                        If True, will label nodes at which old and new
                        tracings have been joined with tags ("Joined from ..."
                        and "Joined with ...") and with a lower confidence of
                        1.
    sid_from_nodes :    bool, optional
                        If True and the to-be-merged neuron has a "skeleton_id"
                        column it will be used to set the ``source_id`` upon
                        uploading new branches. This is relevant if your neuron
                        is a virtual chimera of several neurons: in order to
                        preserve provenance (i.e. correctly associating each
                        node with a ``source_id`` origin) you should make
                        sure that your neuron is

    Returns
    -------
    Nothing
                        If all went well.
    dict
                        If something failed, returns server responses with
                        error logs.

    Examples
    --------
    Setup

    >>> import fafbseg
    >>> import pymaid

    >>> # Set up connections to manual and autoseg CATMAID
    >>> manual = pymaid.CatmaidInstance('URL', 'HTTP_USER', 'HTTP_PW', 'API_TOKEN')
    >>> auto = pymaid.CatmaidInstance('URL', 'HTTP_USER', 'HTTP_PW', 'API_TOKEN')

    >>> # Set a segmentation data source
    >>> fafbseg.use_google_storage("https://storage.googleapis.com/fafb-ffn1-20190805/segmentation")

    Merge a neuron from autoseg into v14

    >>> # Fetch the autoseg neuron to transfer to v14
    >>> x = pymaid.get_neuron(267355161, remote_instance=auto)

    >>> # Get the neuron's annotations so that they can be merged too
    >>> x.get_annotations(remote_instance=auto)

    >>> # Start the commit
    >>> # See online documentation for video of merge process
    >>> resp = fafbseg.merge_neuron(x, target_instance=manual)

    """
    if not isinstance(x, pymaid.CatmaidNeuronList):
        if not isinstance(x, pymaid.CatmaidNeuron):
            raise TypeError('Expected pymaid.CatmaidNeuron/List, got "{}"'.format(type(x)))
        x = pymaid.CatmaidNeuronList(x)

    if not isinstance(tag, (str, type(None))):
        raise TypeError('Tag must be string, got "{}"'.format(type(tag)))

    # Check user permissions
    perm = target_instance.fetch(target_instance.make_url('permissions'))
    requ_perm = ['can_annotate', 'can_annotate_with_token', 'can_import']
    miss_perm = [p for p in requ_perm if
                 target_instance.project_id not in perm[0].get(p, [])]

    if miss_perm:
        msg = 'Your lacks permissions: {}. Please contact an administrator'
        raise PermissionError(msg.format(', '.join(miss_perm)))

    pymaid.set_loggers('WARNING')

    # Throttle requests just to play it safe
    # On a bad connection one might have to decrease max_threads further
    target_instance.max_threads = min(target_instance.max_threads, 50)

    # For user convenience, we will do all the stuff that needs user
    # interaction first and then run the automatic merge:

    # Start by find all overlapping fragments
    overlapping = []
    for n in tqdm(x, desc='Pre-processing neuron(s)',
                  leave=False, disable=not use_pbars):
        ol = find_fragments(n,
                            min_node_overlap=min_node_overlap,
                            min_nodes=min_overlap_size,
                            remote_instance=target_instance)

        if ol:
            # Add number of samplers to each neuron
            n_samplers = pymaid.get_sampler_counts(ol,
                                                   remote_instance=target_instance)

            for nn in ol:
                nn.sampler_count = n_samplers[str(nn.skeleton_id)]

        overlapping.append(ol)

    # Now have the user confirm merges before we actually make them
    viewer = pymaid.Viewer(title='Confirm merges')
    viewer.clear()
    overlap_cnf = []
    base_neurons = []
    for n, ol in zip(x, overlapping):
        # This asks user a bunch of questions prior to merge and upload
        ol, bn = _confirm_overlap(n, ol, viewer=viewer)
        overlap_cnf.append(ol)
        base_neurons.append(bn)
    viewer.close()

    for i, (n, ol, bn) in enumerate(zip(x, overlap_cnf, base_neurons)):
        print('Processing neuron "{}" [{}/{}]'.format(n.neuron_name, i, len(x)),
              flush=True)
        # If no overlapping neurons proceed with just uploading.
        if not ol:
            print('No overlapping fragments to merge. Uploading...',
                  end='', flush=True)
            resp = pymaid.upload_neuron(n,
                                        import_tags=import_tags,
                                        import_annotations=True,
                                        import_connectors=True,
                                        remote_instance=target_instance)
            if 'error' in resp:
                return resp

            # Add annotations
            _ = __merge_annotations(n, resp['skeleton_id'], tag, target_instance)

            msg = '\nNeuron "{}" successfully uploaded to target instance as "{}" #{}'
            print(msg.format(n.neuron_name, n.neuron_name, resp['skeleton_id']),
                  flush=True)
            continue

        # Check if there is a duplicate skeleton ID between the to-be-merged
        # neuron and the to-merge-into neurons
        if n.skeleton_id in ol.skeleton_id:
            print('Fixing duplicate skeleton IDs.',
                  flush=True)
            n.skeleton_id += 'a'
            n._clear_temp_attributes()

        # Check if there are any duplicate node IDs between neuron ``x`` and the
        # overlapping fragments and create new IDs for ``x`` if necessary
        duplicated = n.nodes[n.nodes.treenode_id.isin(ol.nodes.treenode_id.values)]
        if not duplicated.empty:
            print('Duplicate node IDs found. Regenerating node tables... ',
                  end='', flush=True)
            max_ix = max(ol.nodes.treenode_id.max(), n.nodes.treenode_id.max()) + 1
            new_ids = range(max_ix, max_ix + duplicated.shape[0])
            id_map = {old: new for old, new in zip(duplicated.treenode_id, new_ids)}
            n.nodes['treenode_id'] = n.nodes.treenode_id.map(lambda n: id_map.get(n, n))
            n.nodes['parent_id'] = n.nodes.parent_id.map(lambda n: id_map.get(n, n))
            n.connectors['treenode_id'] = n.connectors.treenode_id.map(lambda n: id_map.get(n, n))
            print('Done.', flush=True)

        # Combining the fragments into a single neuron is actually non-trivial:
        # 1. Collapse nodes of our input neuron `x` into within-distance nodes
        #    in the overlapping fragments (never the other way around!)
        # 2. At the same time keep connectivity (i.e. edges) of the input-neuron
        # 3. Keep track of the nodes' provenance (i.e. the contractions)
        #
        # In addition there are a lot of edge-cases to consider. For example:
        # - multiple nodes collapsing onto the same node
        # - nodes of overlapping fragments that are close enough to be collapsed
        #   (e.g. orphan synapse nodes)

        # Keep track of original skeleton IDs
        for a in ol + n:
            # Original skeleton of each node
            a.nodes['origin_skeletons'] = a.skeleton_id
            # Original skeleton of each connector
            a.connectors['origin_skeletons'] = a.skeleton_id

        print('Generating union of all fragments... ', end='', flush=True)
        union, new_edges, collapsed_into = collapse_nodes2(n, ol,
                                                           limit=merge_limit,
                                                           base_neuron=bn)
        print('Done.', flush=True)

        print('Extracting new nodes to upload... ', end='', flush=True)
        # Now we have to break the neuron into "new" fragments that we can upload
        # First get the new and old nodes
        new_nodes = union.nodes[union.nodes.origin_skeletons == n.skeleton_id].treenode_id.values
        old_nodes = union.nodes[union.nodes.origin_skeletons != n.skeleton_id].treenode_id.values

        # Now remove the already existing nodes from the union
        only_new = pymaid.subset_neuron(union, new_nodes)

        # And then break into continuous fragments for upload
        frags = pymaid.break_fragments(only_new)
        print('Done.', flush=True)

        # Also get the new edges we need to generate
        to_stitch = new_edges[~new_edges.parent_id.isnull()]

        # We need this later -> no need to compute this for every uploaded fragment
        cond1b = to_stitch.treenode_id.isin(old_nodes)
        cond2b = to_stitch.parent_id.isin(old_nodes)

        # Now upload each fragment and keep track of new node IDs
        tn_map = {}
        for f in tqdm(frags, desc='Uploading new tracings', leave=False, disable=not use_pbars):
            # In cases of complete merging into existing neurons, the fragment
            # will have no nodes
            if f.n_nodes < 1:
                continue

            # Check if fragment is a "linker" and as such can not be skipped
            lcond1 = np.isin(f.nodes.treenode_id.values,
                             new_edges.treenode_id.values)
            lcond2 = np.isin(f.nodes.treenode_id.values,
                             new_edges.parent_id.values)

            # If not linker, check skip conditions
            if sum(lcond1) + sum(lcond2) <= 1:
                if f.cable_length < min_upload_size:
                    continue
                if f.n_nodes < min_upload_nodes:
                    continue

            # Collect origin info for this neuron
            source_info = {'source_type': 'segmentation'}

            if not sid_from_nodes or 'skeleton_id' not in f.nodes.columns:
                source_info['source_id'] = int(n.skeleton_id)
            else:
                if f.nodes.skeleton_id.unique().shape[0] == 1:
                    skid = f.nodes.skeleton_id.unique()[0]
                else:
                    print('Warning: uploading chimera fragment with multiple '
                          'skeleton IDs! Using largest contributor ID.')
                    # Use the skeleton ID that has the most nodes
                    by_skid = f.nodes.groupby('skeleton_id').x.count()
                    skid = by_skid.sort_values(ascending=False).index.values[0]

                source_info['source_id'] = int(skid)

            if not isinstance(getattr(n, '_remote_instance'), type(None)):
                source_info['source_project_id'] = n._remote_instance.project_id
                source_info['source_url'] = n._remote_instance.server

            resp = pymaid.upload_neuron(f,
                                        import_tags=import_tags,
                                        import_annotations=False,
                                        import_connectors=True,
                                        remote_instance=target_instance,
                                        **source_info)

            # Stop if there was any error while uploading
            if 'error' in resp:
                return resp

            # Collect old -> new node IDs
            tn_map.update(resp['node_id_map'])

            # Now check if we can create any of the new edges by joining nodes
            # Both treenode and parent ID have to be either existing nodes or
            # newly uploaded
            cond1a = to_stitch.treenode_id.isin(tn_map)
            cond2a = to_stitch.parent_id.isin(tn_map)

            to_gen = to_stitch.loc[(cond1a | cond1b) & (cond2a | cond2b)]

            # Join nodes
            for node in to_gen.itertuples():
                # Make sure our base_neuron always come out as winner on top
                if node.treenode_id in bn.nodes.treenode_id.values:
                    winner, looser = node.treenode_id, node.parent_id
                else:
                    winner, looser = node.parent_id, node.treenode_id

                # We need to map winner and looser to the new node IDs
                winner = tn_map.get(winner, winner)
                looser = tn_map.get(looser, looser)

                # And now do the join
                resp = pymaid.join_nodes(winner,
                                         looser,
                                         no_prompt=True,
                                         tag_nodes=label_joins,
                                         remote_instance=target_instance)

                # See if there was any error while uploading
                if 'error' in resp:
                    print('Skipping joining nodes '
                          '{} and {}: {} - '.format(node.treenode_id,
                                                    node.parent_id,
                                                    resp['error']))
                    # Skip changing confidences
                    continue

                # Pop this edge from new_edges and from condition
                new_edges.drop(node.Index, inplace=True)
                cond1b.drop(node.Index, inplace=True)
                cond2b.drop(node.Index, inplace=True)

                # Change node confidences at new join
                if label_joins:
                    new_conf = {looser: 1}
                    resp = pymaid.update_node_confidence(new_conf,
                                                         remote_instance=target_instance)

        # Add annotations
        _ = __merge_annotations(n, bn, tag, target_instance)

        # Update node radii
        if update_radii:
            print('Updating radii of existing nodes... ', end='', flush=True)
            resp = update_node_radii(source=n, target=ol,
                                     remote_instance=target_instance,
                                     limit=merge_limit,
                                     skip_existing=True)
            print('Done.', flush=True)

        print('Neuron "{}" successfully merged into target instance as "{}" #{}'.format(n.neuron_name, bn.neuron_name, bn.skeleton_id),
              flush=True)

    return


def __merge_annotations(n, bn, tag, target_instance):
    """Make sure proper annotations are added."""
    to_add = []
    # Add "{URL} upload {tag} annotation"
    if not isinstance(getattr(n, '_remote_instance'), type(None)):
        u = n._remote_instance.server.split('/')[-1] + ' upload'
        if isinstance(tag, str):
            u += " {}".format(tag)
        to_add.append(u)
    # Existing annotation (the individual fragments would not have inherited them)
    if n.__dict__.get('annotations', []):
        to_add += n.annotations
    # If anything to add
    if to_add:
        _ = pymaid.add_annotations(bn,
                                   to_add,
                                   remote_instance=target_instance)


def collapse_nodes(*x, limit=1, base_neuron=None, priority_nodes=None):
    """Generate the union of a set of neurons.

    This implementation uses edge contraction on the neurons' graph to ensure
    maximum connectivity. Only works if, taken together, the neurons form a
    continuous tree (i.e. you must be certain that they partially overlap).

    Parameters
    ----------
    *x :                CatmaidNeuron/List
                        Neurons to be merged.
    limit :             int, optional
                        Max distance [microns] for nearest neighbour search.
    base_neuron :       skeleton_ID | CatmaidNeuron, optional
                        Neuron to use as template for union. If not provided,
                        the first neuron in the list is used as template!
    priority_nodes :    list-like
                        List of treenode IDs. If provided, these nodes will
                        have priority when pairwise collapsing nodes. If two
                        priority nodes are to be collapsed, a new edge between
                        them is created instead.

    Returns
    -------
    core.CatmaidNeuron
                        Union of all input neurons.
    collapsed_nodes :   dict
                        Map of collapsed nodes::

                            NodeA -collapsed-into-> NodeB

    new_edges :         list
                        List of newly added edges::

                            [[NodeA, NodeB], ...]

    """
    # Unpack neurons in *args
    x = pymaid.utils._unpack_neurons(x)

    # Make sure we're working on copies and don't change originals
    x = pymaid.CatmaidNeuronList([n.copy() for n in x])

    if isinstance(priority_nodes, type(None)):
        priority_nodes = []

    # This is just check on the off-chance that skeleton IDs are not unique
    # (e.g. if neurons come from different projects) -> this is relevant because
    # we identify the master ("base_neuron") via it's skeleton ID
    skids = [n.skeleton_id for n in x]
    if len(skids) > len(np.unique(skids)):
        raise ValueError('Duplicate skeleton IDs found in neurons to be merged. '
                         'Try manually assigning unique skeleton IDs.')

    if any([not isinstance(n, pymaid.CatmaidNeuron) for n in x]):
        raise TypeError('Input must only be CatmaidNeurons/List')

    if len(x) < 2:
        raise ValueError('Need at least 2 neurons to make a union!')

    # Convert distance threshold from microns to nanometres
    limit *= 1000

    # First make a weak union by simply combining the node tables
    union_simple = pymaid.stitch_neurons(x, method='NONE', master=base_neuron)

    # Check for duplicate node IDs
    if any(union_simple.nodes.treenode_id.duplicated()):
        raise ValueError('Duplicate node IDs found.')

    # Map priority nodes -> this will speed things up later
    is_priority = {n: True for n in priority_nodes}

    # Go over each pair of fragments and check if they can be collapsed
    comb = itertools.combinations(x, 2)
    collapse_into = {}
    new_edges = []
    for c in comb:
        tree = pymaid.neuron2KDTree(c[0], tree_type='c', data='treenodes')

        # For each node in master get the nearest neighbor in minion
        coords = c[1].nodes[['x', 'y', 'z']].values
        nn_dist, nn_ix = tree.query(coords, k=1, distance_upper_bound=limit)

        clps_left = c[0].nodes.iloc[nn_ix[nn_dist <= limit]].treenode_id.values
        clps_right = c[1].nodes.iloc[nn_dist <= limit].treenode_id.values
        clps_dist = nn_dist[nn_dist <= limit]

        for i, (n1, n2, d) in enumerate(zip(clps_left, clps_right, clps_dist)):
            if is_priority.get(n1, False):
                # If both nodes are priority nodes, don't collapse
                if is_priority.get(n2, False):
                    new_edges.append([n1, n2, d])
                    # continue
                else:
                    collapse_into[n2] = n1
            else:
                collapse_into[n1] = n2

    # Get the graph
    G = union_simple.graph

    # Add the new edges to graph
    G.add_weighted_edges_from(new_edges)

    # Using an edge list is much more efficient than an adjacency matrix
    E = nx.to_pandas_edgelist(G)

    # All nodes that collapse into other nodes need to have weight set to
    # float("inf") to de-prioritize them when generating the minimum spanning
    # tree later
    clps_nodes = set(collapse_into.keys())
    E.loc[(E.source.isin(clps_nodes)) | (E.target.isin(clps_nodes)), 'weight'] = float('inf')

    # Now map collapsed nodes onto the nodes they collapsed into
    E['target'] = E.target.map(lambda x: collapse_into.get(x, x))
    E['source'] = E.source.map(lambda x: collapse_into.get(x, x))

    # Make sure no self loops after collapsing. This happens if two adjacent
    # nodes collapse onto the same target node
    E = E[E.source != E.target]

    # Turn this back into a graph
    G_clps = nx.from_pandas_edgelist(E, edge_attr='weight')

    # Make sure that we are fully connected
    if not nx.is_connected(G_clps):
        raise ValueError('Neuron still fragmented after collapsing nodes. '
                         'Try increasing the `limit` parameter.')

    # Under certain conditions, collapsing nodes will introduce cycles:
    # Consider for example a graph: A->B->C D->E->F
    # Collapsing A and C into D will create a loop between B<->D
    # To fix this we have to create a minimum spanning tree.
    # In doing so, we need to prioritize existing edges over new edges
    # otherwise we would have to cut existing neurons -> this is why we set
    # weight of new edges to float("inf") earlier on

    # Generate the tree
    tree = nx.minimum_spanning_tree(G_clps.to_undirected(as_view=True))

    # Add properties to nodes
    survivors = np.unique(E[['source', 'target']])
    props = union_simple.nodes.set_index('treenode_id').loc[survivors]
    nx.set_node_attributes(tree, props.to_dict(orient='index'))

    # Recreate neuron
    union = pymaid.graph.nx2neuron(tree,
                                   neuron_name=union_simple.neuron_name,
                                   skeleton_id=union_simple.skeleton_id)

    # Add tags back on
    for n in x:
        union.tags.update({k: union.tags.get(k, []) + [collapse_into.get(a, a) for a in v] for k, v in n.tags.items()})

    # Add connectors back on
    union.connectors = x.connectors.drop_duplicates(subset='connector_id')
    union.connectors.treenode_id = union.connectors.treenode_id.map(lambda x: collapse_into.get(x, x))

    # Return the last survivor
    return union, collapse_into, new_edges


def collapse_nodes2(A, B, limit=2, base_neuron=None):
    """Merge neuron A into neuron(s) B creating a union of both.

    This implementation uses edge contraction on the neurons' graph to ensure
    maximum connectivity. Only works if the fragments collectively form a
    continuous tree (i.e. you must be certain that they partially overlap).

    Parameters
    ----------
    A :                 CatmaidNeuron
                        Neuron to be collapsed into neurons B.
    B :                 CatmaidNeuronList
                        Neurons to collapse neuron A into.
    limit :             int, optional
                        Max distance [microns] for nearest neighbour search.
    base_neuron :       skeleton_ID | CatmaidNeuron, optional
                        Neuron from B to use as template for union. If not
                        provided, the first neuron in the list is used as
                        template!

    Returns
    -------
    core.CatmaidNeuron
                        Union of all input neurons.
    new_edges :         pandas.DataFrame
                        Subset of the ``.nodes`` table that represent newly
                        added edges.
    collapsed_nodes :   dict
                        Map of collapsed nodes::

                            NodeA -collapsed-into-> NodeB

    """
    if isinstance(A, pymaid.CatmaidNeuronList):
        if len(A) == 1:
            A = A[0]
        else:
            A = pymaid.stitch_neurons(A, method="NONE")
    elif not isinstance(A, pymaid.CatmaidNeuron):
        raise TypeError('`A` must be a CatmaidNeuron, got "{}"'.format(type(A)))

    if isinstance(B, pymaid.CatmaidNeuron):
        B = pymaid.CatmaidNeuronList(B)
    elif not isinstance(B, pymaid.CatmaidNeuronList):
        raise TypeError('`B` must be a CatmaidNeuronList, got "{}"'.format(type(B)))

    # This is just check on the off-chance that skeleton IDs are not unique
    # (e.g. if neurons come from different projects) -> this is relevant because
    # we identify the master ("base_neuron") via it's skeleton ID
    skids = [n.skeleton_id for n in B + A]
    if len(skids) > len(np.unique(skids)):
        raise ValueError('Duplicate skeleton IDs found. Try manually assigning '
                         'unique skeleton IDs.')

    # Convert distance threshold from microns to nanometres
    limit *= 1000

    # Before we start messing around, let's make sure we can keep track of
    # the origin of each node
    for n in B + A:
        n.nodes['origin_skeletons'] = n.skeleton_id

    # First make a weak union by simply combining the node tables
    union_simple = pymaid.stitch_neurons(B + A, method='NONE', master=base_neuron)

    # Check for duplicate node IDs
    if any(union_simple.nodes.treenode_id.duplicated()):
        raise ValueError('Duplicate node IDs found.')

    # Find nodes in A to be merged into B
    tree = pymaid.neuron2KDTree(B, tree_type='c', data='treenodes')

    # For each node in A get the nearest neighbor in B
    coords = A.nodes[['x', 'y', 'z']].values
    nn_dist, nn_ix = tree.query(coords, k=1, distance_upper_bound=limit)

    # Find nodes that are close enough to collapse
    collapsed = A.nodes.loc[nn_dist <= limit].treenode_id.values
    clps_into = B.nodes.iloc[nn_ix[nn_dist <= limit]].treenode_id.values

    clps_map = {n1: n2 for n1, n2 in zip(collapsed, clps_into)}

    # The fastest way to collapse is to work on the edge list
    E = nx.to_pandas_edgelist(union_simple.graph)

    # Keep track of which edges were collapsed -> we will use this as weight
    # later on to prioritize existing edges over newly generated ones
    E['is_new'] = 1
    E.loc[(E.source.isin(B.nodes.treenode_id.values)) | (E.target.isin(B.nodes.treenode_id.values)), 'is_new'] = 0

    # Now map collapsed nodes onto the nodes they collapsed into
    E['target'] = E.target.map(lambda x: clps_map.get(x, x))
    E['source'] = E.source.map(lambda x: clps_map.get(x, x))

    # Make sure no self loops after collapsing. This happens if two adjacent
    # nodes collapse onto the same target node
    E = E[E.source != E.target]

    # Remove duplicates. This happens e.g. when two adjaceny nodes merge into
    # two other adjaceny nodes: A->B C->D ----> A/B->C/D
    # By sorting first, we make sure original edges are kept first
    E.sort_values('is_new', ascending=True, inplace=True)

    # Because edges may exist in both directions (A->B and A<-B) we have to
    # generate a column that's agnostic to directionality using frozensets
    E['edge'] = E[['source', 'target']].apply(frozenset, axis=1)
    E.drop_duplicates(['edge'], keep='first', inplace=True)

    # Regenerate graph from these new edges
    G = nx.Graph()
    G.add_weighted_edges_from(E[['source', 'target', 'is_new']].values.astype(int))

    # At this point there might still be disconnected pieces -> we will create
    # separate neurons for each tree
    props = union_simple.nodes.loc[union_simple.nodes.treenode_id.isin(G.nodes)].set_index('treenode_id')
    nx.set_node_attributes(G, props.to_dict(orient='index'))
    fragments = []
    for n in nx.connected_components(G):
        c = G.subgraph(n)
        tree = nx.minimum_spanning_tree(c)
        fragments.append(pymaid.graph.nx2neuron(tree,
                                                neuron_name=base_neuron.neuron_name,
                                                skeleton_id=base_neuron.skeleton_id))
    fragments = pymaid.CatmaidNeuronList(fragments)

    if len(fragments) > 1:
        # Now heal those fragments using a minimum spanning tree
        union = pymaid.stitch_neurons(*fragments, method='ALL')
    else:
        union = fragments[0]

    # Reroot to base neuron's root
    union.reroot(base_neuron.root[0], inplace=True)

    # Add tags back on
    union.tags.update(union_simple.tags)

    # Add connectors back on
    union.connectors = union_simple.connectors.drop_duplicates(subset='connector_id').copy()
    union.connectors.loc[:, 'treenode_id'] = union.connectors.treenode_id.map(lambda x: clps_map.get(x, x))

    # Find the newly added edges (existing edges should not have been modified
    # - except for changing direction due to reroot)
    # The basic logic here is that new edges were only added between two
    # previously separate skeletons, i.e. where the skeleton ID changes between
    # parent and child node
    node2skid = union_simple.nodes.set_index('treenode_id').skeleton_id.to_dict()
    union.nodes['parent_skeleton'] = union.nodes.parent_id.map(node2skid)
    new_edges = union.nodes[union.nodes.origin_skeletons != union.nodes.parent_skeleton]
    # Remove root edges
    new_edges = new_edges[~new_edges.parent_id.isnull()]

    return union, new_edges, clps_map


def update_node_radii(source, target, remote_instance, limit=2, skip_existing=True):
    """Update node radii in target neuron from their nearest neighbor in source neuron.

    Parameters
    ----------
    source :            CatmaidNeuron
                        Neuron which node radii to use to update target neuron.
    target :            CatmaidNeuron
                        Neuron which node radii to update.
    remote_instance :   CatmaidInstance
                        Catmaid instance in which ``target`` lives.
    limit :             int, optional
                        Max distance [um] between source and target neurons for
                        nearest neighbor search.
    skip_existing :     bool, optional
                        If True, will skip nodes in ``source`` that already have
                        a radius >0.

    Returns
    -------
    dict
                        Server response.

    """
    if not isinstance(source, (pymaid.CatmaidNeuron, pymaid.CatmaidNeuronList)):
        raise TypeError('Expected CatmaidNeuron/List, got "{}"'.format(type(source)))

    if not isinstance(target, (pymaid.CatmaidNeuron, pymaid.CatmaidNeuronList)):
        raise TypeError('Expected CatmaidNeuron/List, got "{}"'.format(type(target)))

    # Turn limit from microns to nanometres
    limit *= 1000

    # First find the closest neighbor within distance limit for each node in target
    # Find nodes in A to be merged into B
    tree = pymaid.neuron2KDTree(source, tree_type='c', data='treenodes')

    nodes = target.nodes
    if skip_existing:
        # Extract nodes without a radius
        nodes = nodes[nodes.radius <= 0]

    # For each node in A get the nearest neighbor in B
    coords = nodes[['x', 'y', 'z']].values
    nn_dist, nn_ix = tree.query(coords, k=1, distance_upper_bound=limit)

    # Find nodes that are close enough to collapse
    tn_ids = nodes.loc[nn_dist <= limit].treenode_id.values
    new_radii = source.nodes.iloc[nn_ix[nn_dist <= limit]].radius.values

    return pymaid.update_radii(dict(zip(tn_ids, new_radii)),
                               remote_instance=remote_instance)


def _confirm_overlap(x, fragments, viewer=None):
    """Show dialogs to confirm overlapping fragments."""
    print('{}: {} overlapping fragments found'.format(x.neuron_name, len(fragments)))
    if fragments:
        fragments.sort_values('n_nodes')
        # Have user inspect fragments
        # Show larger fragments in 3d viewer
        if any(fragments.n_nodes > 10):
            # Generate a summary
            large_frags = fragments[fragments.n_nodes > 10]
            s = large_frags.summary(add_cols=['overlap_score'])[['neuron_name',
                                                                 'skeleton_id',
                                                                 'n_nodes',
                                                                 'n_connectors',
                                                                 'overlap_score']]
            # Show and let user decide which ones to merge
            if not viewer:
                viewer = pymaid.Viewer(title='Check overlap')
            # Make sure viewer is actually visible and cleared
            viewer.show()
            viewer.clear()
            # Add original skeleton
            viewer.add(x, color='w')
            viewer.add(large_frags)
            viewer.picking = True
            viewer._picking_text.visible = True
            viewer.show_legend = True

            # Print summary
            print('Large (>10 nodes) overlapping fragments:')
            print(s.to_string(index=False, show_dimensions=False))

            msg = """
            Please check these large fragments for overlap and deselect
            neurons that you DO NOT want to have merged by clicking on
            their names in the legend.
            Hit ENTER when you are ready to proceed or CTRL-C to cancel.
            """

            try:
                _ = input(msg)
            except KeyboardInterrupt:
                raise KeyboardInterrupt('Merge process aborted by user.')
            except BaseException:
                raise

            # Remove deselected fragments
            # Mind you not all fragments are on viewer - this is why we remove
            # neurons that has been hidden
            fragments = fragments[~np.isin(fragments.skeleton_id, viewer.invisible)]

    # Now ask for smaller fragments via CLI
    if fragments:
        s = fragments.summary(add_cols=['overlap_score',
                                        'sampler_count'])[['neuron_name',
                                                           'skeleton_id',
                                                           'n_nodes',
                                                           'n_connectors',
                                                           'sampler_count',
                                                           'overlap_score']]

        # Ask user which neuron should be merged
        msg = """
        Please check the fragments that potentially overlap with the input neuron (white).
        Deselect those that should NOT be merged using the arrows keys.
        Hit ENTER when you are ready to proceed or CTRL-C to abort
        """
        print(msg)

        msg = s.to_string(index=False).split('\n')[0]

        s_str = s.to_string(index=False, show_dimensions=False, header=False)
        choices = [(v, i) for i, v in enumerate(s_str.split('\n'))]
        q = [inquirer.Checkbox(name='selection',
                               message=msg,
                               choices=choices,
                               default=list(range(len(choices))))]

        # Ask the question
        selection = inquirer.prompt(q, theme=GreenPassion()).get('selection')

        if isinstance(selection, type(None)):
            raise SystemExit('Merge process aborted by user.')

        # Remove fragments that are not selected
        if selection:
            fragments = fragments[selection]
        else:
            # If no selection, remove all neurons from the list
            fragments = fragments[:0]

    # If no overlapping fragments (either none from the start or all removed
    # during filtering) ask if just proceed with upload
    if not fragments:
        print('No overlapping fragments to be merged into in target instance.')
        msg = 'Proceed with just uploading this neuron?'
        q = [inquirer.Confirm(name='confirm', message=msg)]
        confirm = inquirer.prompt(q, theme=GreenPassion()).get('confirm')

        if not confirm:
            raise SystemExit('Merge process aborted by user.')

        base_neuron = None
    # If any fragments left, ask for base neuron
    else:
        # Ask user which neuron to use as merge target
        s = fragments.summary(add_cols=['overlap_score',
                                        'sampler_count'])[['neuron_name',
                                                           'skeleton_id',
                                                           'n_nodes',
                                                           'n_connectors',
                                                           'sampler_count',
                                                           'overlap_score']]

        msg = """
        Above fragments and your input neuron will be merged into a single neuron.
        All annotations will be preserved but only the neuron used as merge target
        will keep its name and skeleton ID.
        Please select the neuron you would like to use as merge target!
        """ + s.to_string(index=False).split('\n')[0]
        print(msg)

        s_str = s.to_string(index=False, show_dimensions=False, header=False)
        choices = [(v, i) for i, v in enumerate(s_str.split('\n'))]
        q = [inquirer.List(name='base_neuron',
                           message='Choose merge target',
                           choices=choices)]
        # Ask the question
        bn = inquirer.prompt(q, theme=GreenPassion()).get('base_neuron')

        if isinstance(bn, type(None)):
            raise ValueError("Merge aborted by user")

        base_neuron = fragments[bn]

        # Some safeguards:
        # Check if we would delete any samplers
        cond1 = s.skeleton_id != base_neuron.skeleton_id
        cond2 = s.sampler_count > 0
        has_sampler = s[cond1 & cond2]
        if not has_sampler.empty:
            print("Merging selected fragments would delete reconstruction "
                  "samplers on the following neurons:")
            print(has_sampler)
            q = [inquirer.Confirm(name='confirm', message='Proceed anyway?')]
            confirm = inquirer.prompt(q, theme=GreenPassion())['confirm']

            if not confirm:
                raise SystemExit('Merge process aborted by user.')

        # Check if we would generate any 2-soma neurons
        has_soma = [not isinstance(s, type(None)) for s in fragments.soma]
        if sum(has_soma) > 1:
            print('Merging the selected fragments would generate a neuron  '
                  'with two somas!')
            q = [inquirer.Confirm(name='confirm', message='Proceed anyway?')]
            confirm = inquirer.prompt(q, theme=GreenPassion())['confirm']

            if not confirm:
                raise SystemExit('Merge process aborted by user.')

    return fragments, base_neuron
