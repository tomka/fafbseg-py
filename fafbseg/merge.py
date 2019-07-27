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
from pymaid.cache import never_cache

from .search import find_fragments


@never_cache
def merge_neuron(x, target_instance, merge_limit=1, update_radii=True,
                 overlapping=None):
    """Merge neuron into target instance.

    This function will attempt to:
        1. Find fragments in ``target_instance`` that overlap with ``x``
           using the brainmaps API.
        2. Generate a union of these fragments and ``x``.
        3. Make a differential upload of the union leaving existing nodes
           untouched.
        4. Join uploaded and existing fragments into a single continuous
           neuron.

    Parameters
    ----------
    x :                 pymaid.CatmaidNeuron
                        Neuron/Fragment to commit to ``target_instance``.
    target_instance :   pymaid.CatmaidInstance
                        Target Catmaid instance to commit the neuron to.
    merge_limit :       int, optional
                        Distance threshold [um] for generating union of ``x``
                        and overlapping fragments in target instance.
    update_radii :      bool, optional
                        If True, will use radii in ``x`` to update radii of
                        overlapping fragments if (and only if) the nodes
                        do not currently have a radius (i.e. radius<=0).
    overlapping :       list of skeleton IDs | CatmaidNeuronList, optional
                        If you already know which neurons in the target
                        instance are overlapping with ``x`` you can pass them
                        explicitly and we can skip the step of searching for
                        and confirming overlapping fragments.

    Returns
    -------
    Nothing
                        If all went well.
    dict
                        If something failed, returns server responses with
                        error logs.

    """
    if not isinstance(x, pymaid.CatmaidNeuron):
        raise TypeError('Expected pymaid.CatmaidNeuron, got "{}"'.format(type(x)))

    # If overlapping fragments are provided by user
    if overlapping:
        if not isinstance(overlapping, pymaid.CatmaidNeuronList):
            overlapping = pymaid.get_neurons(overlapping,
                                             remote_instance=target_instance)
        ol = overlapping
    else:
        # First get potential overlapping fragments in target_instance
        ol = find_fragments(x, remote_instance=target_instance)

        # If no overlapping neurons proceed with just uploading.
        if not ol:
            q = 'No overlapping fragments found in target instance. Proceed with ' \
                'simply uploading the input neuron? [Y/N]'
            answer = ''
            while answer.lower() not in ['y', 'n']:
                answer = input(q)

            if answer.lower() == 'n':
                return

            resp = pymaid.upload_neuron(x,
                                        import_tags=True,
                                        import_annotations=False,
                                        import_connectors=True,
                                        remote_instance=target_instance)

            if 'error' in resp:
                return resp

            return

        # Have user inspect larger fragments
        ol.sort_values('n_nodes')
        q = str(ol.summary()[['neuron_name', 'skeleton_id', 'n_nodes', 'n_connectors']])
        print('{} overlapping fragments found:'.format(len(ol)))
        print(q)
        if any(ol.n_nodes > 1):
            # Show and let user decide which ones to merge
            v = pymaid.Viewer(title='Commit check')
            v.add(x, color='w')
            v.add(ol[ol.n_nodes > 10])
            v.toggle_picking()
            v.show_legend = True

            # Ask user which neuron should survive
            q = "Please check the fragments that potentially overlap with the input " \
                "neuron (white).\nDeselect those that should not be merged by clicking " \
                "on their names in the legend. Only neurons with >10 nodes are shown." \
                "\nHit ENTER when ready to proceed or 'Q' to cancel."

            answer = input(q)

            v.close()

            if 'q' in answer.lower():
                return

            # Remove those that are not selected (keep in mind that some neurons
            # won't even be in the viewer)
            ol = pymaid.CatmaidNeuronList([n for n in ol if n.skeleton_id not in v.invisible])

    # If no overlapping fragments remain, proceed to upload
    if not ol:
        q = 'It appears you do not want any overlapping fragments to be merged' \
            'Proceed with simply uploading the input neuron? [Y/N]'
        answer = ''
        while answer.lower() not in ['y', 'n']:
            answer = input(q)

        if answer.lower() == 'n':
            return

        resp = pymaid.upload_neuron(x,
                                    import_tags=True,
                                    import_annotations=False,
                                    import_connectors=True,
                                    remote_instance=target_instance)

        return resp

    # Check if there are any duplicate node IDs between neuron ``x`` and the
    # overlapping fragments and create new IDs for ``x`` if necessary
    duplicated = x.nodes[x.nodes.treenode_id.isin(ol.nodes.treenode_id.values)]
    if not duplicated.empty:
        print('Duplicate node IDs found. Regenerating node tables... ',
              end='', flush=True)
        max_ix = max(ol.nodes.treenode_id.max(), x.nodes.treenode_id.max()) + 1
        new_ids = range(max_ix, max_ix + duplicated.shape[0])
        id_map = {old: new for old, new in zip(duplicated.treenode_id, new_ids)}
        x.nodes['treenode_id'] = x.nodes.treenode_id.map(lambda n: id_map.get(n, n))
        x.nodes['parent_id'] = x.nodes.parent_id.map(lambda n: id_map.get(n, n))
        x.connectors['treenode_id'] = x.connectors.treenode_id.map(lambda n: id_map.get(n, n))
        print('Done.', flush=True)

    # Some safeguards
    if sum([not isinstance(s, type(None)) for s in ol.soma]):
        q = 'Merging the selected fragments would generate a neuron with two ' \
            'somas! Proceed anyway? [Y/N]'
        answer = ''
        while answer.lower() not in ['y', 'n']:
            answer = input(q)

        if answer.lower() == 'n':
            return

    # Ask user which neuron should survive
    s = str(ol.summary()[['neuron_name', 'skeleton_id', 'n_nodes', 'n_connectors']])
    print('Remaining fragments:')
    print(s)
    q = "\nAbove fragments and your input neuron will be merged into a " \
        "single neuron.\nAll annotations will be preserved but only the neuron " \
        "used as merge target will keep its name and skeleton ID.\nPlease enter " \
        "the index of the neuron you would like to use as merge target! [0] "
    inp = input(q)

    # Now make union of these fragments
    if not inp:
        base_neuron = ol[0]
    else:
        base_neuron = ol[int(inp)]

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
    for n in ol + x:
        # Original skeleton of each node
        n.nodes['origin_skeletons'] = n.skeleton_id
        # Original skeleton of each connector
        n.connectors['origin_skeletons'] = n.skeleton_id

    print('Generating union of all fragments... ', end='', flush=True)
    union, new_edges, collapsed_into = collapse_nodes2(x, ol,
                                                       limit=merge_limit,
                                                       base_neuron=base_neuron)
    print('Done.', flush=True)

    print('Extracting new nodes to upload... ', end='', flush=True)
    # Now we have to break the neuron into "new" fragments that we can upload
    # First remove the already existing nodes from the union
    new_nodes = union.nodes[union.nodes.origin_skeletons == x.skeleton_id].treenode_id.values
    only_new = pymaid.subset_neuron(union, new_nodes)

    # Break into continuous fragments for upload
    frags = pymaid.break_fragments(only_new)
    print('Done.', flush=True)

    # Rename them (helps with debugging if something went wrong)
    for i, f in enumerate(frags):
        f.neuron_name = '{} (upload fragment {})'.format(x.neuron_name, i)

    # Now upload each fragment and keep track of new node IDs
    tn_map = {}
    for n in tqdm(frags, desc='Uploading new tracings', leave=False):
        resp = pymaid.upload_neuron(n,
                                    import_tags=True,
                                    import_annotations=False,
                                    import_connectors=True,
                                    remote_instance=target_instance)

        # Stop if there was any error while uploading
        if 'error' in resp:
            return resp

        # Collect old -> new node IDs
        tn_map.update(resp['node_id_map'])

    # Join nodes
    to_stitch = new_edges[~new_edges.parent_id.isnull()]
    for n in tqdm(to_stitch.itertuples(),
                  desc='Stitching',
                  total=to_stitch.shape[0],
                  leave=False):
        # Make sure our base_neuron always come out as winner on top
        if n.treenode_id in base_neuron.nodes.treenode_id.values:
            winner, looser = n.treenode_id, n.parent_id
        else:
            winner, looser = n.parent_id, n.treenode_id

        # We need to map winner and looser to the new node IDs
        winner = tn_map.get(winner, winner)
        looser = tn_map.get(looser, looser)

        resp = pymaid.join_nodes(winner,
                                 looser,
                                 no_prompt=True,
                                 remote_instance=target_instance)

        # Stop if there was any error while uploading
        if 'error' in resp:
            print('Error automatically joining nodes {} and {}: {}'.format(n.treenode_id, n.parent_id, resp['error']))

    # Update node radii
    if update_radii:
        print('Updating radii of existing nodes... ', end='', flush=True)
        resp = update_node_radii(source=x, target=ol,
                                 remote_instance=target_instance,
                                 limit=merge_limit,
                                 skip_existing=True)
        print('Done.', flush=True)

    print('Success!')

    return


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
        raise ValueError('Duplicate skeleton IDs found. Try manually assigning '
                         'unique skeleton IDs.')

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
    """Merge neuron A into neuron(s) B creating a union.

    This implementation uses edge contraction on the neurons' graph to ensure
    maximum connectivity. Only works if the neurons collectively form a
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
    if not isinstance(A, pymaid.CatmaidNeuron):
        raise TypeError('`A` must be a CatmaidNeuron, got "{}"'.format(type(A)))

    if not isinstance(B, pymaid.CatmaidNeuronList):
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
    union.connectors = union_simple.connectors.drop_duplicates(subset='connector_id')
    union.connectors['treenode_id'] = union.connectors.treenode_id.map(lambda x: clps_map.get(x, x))

    # Find the newly added edges (existing edges should not have been modified
    # - except for changing direction due to reroot)
    # The basic logic here is that new edges were only added between two
    # previously separate skeletons, i.e. where the skeleton ID changes between
    # parent and child node
    node2skid = union_simple.nodes.set_index('treenode_id').skeleton_id.to_dict()
    union.nodes['parent_skeleton'] = union.nodes.parent_id.map(node2skid)
    new_edges = union.nodes[union.nodes.skeleton_id != union.nodes.parent_skeleton]
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

