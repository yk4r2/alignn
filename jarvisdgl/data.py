"""Jarvis-dgl data loaders and DGLGraph utilities."""
import functools
import json
import os
import random
from collections import Counter, defaultdict
from typing import Dict, List, Set, Tuple

import dgl
import numpy as np
import torch
from jarvis.core.atoms import Atoms
from jarvis.core.graphs import Graph
from jarvis.core.specie import Specie
from jarvis.db.figshare import data as jdata
from pymatgen.analysis.local_env import VoronoiNN
from pymatgen.core.structure import Structure as PymatgenStructure
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader
from tqdm import tqdm

BASIC_FEATURES = [
    "Z",
    "coulmn",
    "row",
    "X",
    "atom_rad",
    "nsvalence",
    "npvalence",
    "ndvalence",
    "nfvalence",
    "first_ion_en",
    "elec_aff",
]


MIT_ATOM_FEATURES_JSON = os.path.join(
    os.path.dirname(__file__), "atom_init.json"
)


def prepare_dgl_batch(batch, device=None, non_blocking=False):
    """Send batched dgl graph to device."""
    g, t = batch
    batch = (g.to(device), t.to(device))

    return batch


def dgl_crystal(
    atoms: Atoms,
    primitive: bool = False,
    cutoff: float = 8,
    enforce_c_size: float = 5,
    atom_features="atomic_number",
):
    """Get DGLGraph from atoms, go through jarvis.core.graph."""
    feature_sets = ("atomic_number", "basic", "cfid")
    if atom_features not in feature_sets:
        raise NotImplementedError(
            f"atom features must be one of {feature_sets}"
        )

    jgraph = Graph.from_atoms(
        atoms,
        features=atom_features,
        get_prim=primitive,
        max_cut=cutoff,
        enforce_c_size=enforce_c_size,
    )

    # weight is currently
    #  `adj = variance * np.exp(-bond_distance / lengthscale)`
    g = dgl.from_networkx(jgraph.to_networkx(), edge_attrs=["weight"])
    g.edata["bondlength"] = g.edata["weight"].type(torch.FloatTensor)
    del g.edata["weight"]

    g.ndata["atom_features"] = torch.tensor(jgraph.node_attributes).type(
        torch.FloatTensor
    )

    return g


@functools.lru_cache(maxsize=None)
def _get_node_attributes(species: str, atom_features: str = "atomic_number"):

    feature_sets = ("atomic_number", "basic", "cfid", "mit")

    if atom_features not in feature_sets:
        raise NotImplementedError(
            f"atom features must be one of {feature_sets}"
        )

    if atom_features == "cfid":
        return Specie(species).get_descrp_arr
    elif atom_features == "atomic_number":
        return [Specie(species).element_property("Z")]
    elif atom_features == "basic":
        return [
            Specie(species).element_property(prop) for prop in BASIC_FEATURES
        ]
    elif atom_features == "mit":
        # load from json, key by atomic number
        key = str(Specie(species).element_property("Z"))
        with open(MIT_ATOM_FEATURES_JSON, "r") as f:
            i = json.load(f)
        return i[key]


def canonize_edge(
    src_id: int,
    dst_id: int,
    src_image: Tuple[int, int, int],
    dst_image: Tuple[int, int, int],
):
    """Compute canonical edge representation.

    sort vertex ids
    shift periodic images so the first vertex is in (0,0,0) image
    """
    # store directed edges src_id <= dst_id
    if dst_id < src_id:
        src_id, dst_id = dst_id, src_id
        src_image, dst_image = dst_image, src_image

    # shift periodic images so that src is in (0,0,0) image
    if not np.array_equal(src_image, (0, 0, 0)):
        shift = src_image
        src_image = tuple(np.subtract(src_image, shift))
        dst_image = tuple(np.subtract(dst_image, shift))

    assert src_image == (0, 0, 0)

    return src_id, dst_id, src_image, dst_image


def build_undirected_edgedata(
    structure: PymatgenStructure,
    edges: Dict[Tuple[int, int], Set[Tuple[int, int, int]]],
):
    """Build undirected graph data from edge set.

    edges: dictionary mapping (src_id, dst_id) to set of dst_image
    r: cartesian displacement vector from src -> dst
    """
    # second pass: construct *undirected* graph
    u, v, r = [], [], []
    for (src_id, dst_id), images in edges.items():

        for dst_image in images:
            # fractional coordinate for periodic image of dst
            dst_coord = structure[dst_id].frac_coords + dst_image
            # cartesian displacement vector pointing from src -> dst
            d = structure.lattice.get_cartesian_coords(
                dst_coord - structure[src_id].frac_coords
            )
            # add edges for both directions
            for uu, vv, dd in [(src_id, dst_id, d), (dst_id, src_id, -d)]:
                u.append(uu)
                v.append(vv)
                r.append(dd)

    u = torch.tensor(u)
    v = torch.tensor(v)
    r = torch.tensor(r).type(torch.get_default_dtype())

    return u, v, r


def nearest_neighbor_edges(
    structure: PymatgenStructure,
    cutoff: float = 8,
    max_neighbors: int = 12,
):
    """Construct k-NN edge list."""
    # returns List[List[Tuple[site, distance, index, image]]]
    all_neighbors = structure.get_all_neighbors(cutoff)

    # if a site has too few neighbors, increase the cutoff radius
    min_nbrs = min(len(neighborlist) for neighborlist in all_neighbors)
    if min_nbrs < max_neighbors:
        print("extending cutoff radius!")

        # first iteration: set cutoff to cell size
        lat = structure.lattice
        if cutoff < max(lat.a, lat.b, lat.c):
            r_cut = max(lat.a, lat.b, lat.c)

        else:
            # recursive iterations:
            r_cut = 2 * cutoff

        return nearest_neighbor_edges(structure, r_cut, max_neighbors)

    # build up edge list
    # NOTE: currently there's no guarantee that this creates undirected graphs
    # An undirected solution would build the full edge list where nodes are
    # keyed by (index, image), and ensure each edge has a complementary edge

    # indeed, JVASP-59628 is an example of a calculation where this produces
    # a graph where one site has no incident edges!

    # build an edge dictionary u -> v
    # so later we can run through the dictionary
    # and remove all pairs of edges
    # so what's left is the odd ones out
    edges = defaultdict(set)
    for site_idx, neighborlist in enumerate(all_neighbors):

        # sort on distance
        neighborlist = sorted(neighborlist, key=lambda x: x[1])

        distances = np.array([nbr[1] for nbr in neighborlist])
        ids = np.array([nbr[2] for nbr in neighborlist])
        images = np.array([nbr[3] for nbr in neighborlist])

        # find the distance to the k-th nearest neighbor
        max_dist = distances[max_neighbors - 1]

        # keep all edges out to the neighbor shell of the k-th neighbor
        ids = ids[distances <= max_dist]
        images = images[distances <= max_dist]
        distances = distances[distances <= max_dist]

        # keep track of cell-resolved edges
        # to enforce undirected graph construction
        for dst, image in zip(ids, images):
            src_id, dst_id, src_image, dst_image = canonize_edge(
                site_idx, dst, (0, 0, 0), tuple(image)
            )
            edges[(src_id, dst_id)].add(dst_image)

    return edges


def voronoi_edges(structure: PymatgenStructure):
    """Add edges from voronoi nearest neighbors.

    Follow conventions from pymatgen.StructureGraph
    """
    vnn = VoronoiNN(extra_nn_info=False, allow_pathological=True)

    # computing all voronoi polyhedra at once is more efficient
    # but breaks on some structures -- go site-by-site for these
    try:
        all_edge_data = vnn.get_all_nn_info(structure)
    except ValueError:
        # some structures report
        # No Voronoi neighbours found for site - try increasing cutoff
        all_edge_data = [
            vnn.get_nn_info(structure, src) for src in range(len(structure))
        ]

    edges = defaultdict(set)
    for src, edge_data in enumerate(all_edge_data):

        for edge in edge_data:
            src_id, src_image = src, (0, 0, 0)
            dst_id, dst_image = edge["site_index"], edge["image"]

            src_id, dst_id, src_image, dst_image = canonize_edge(
                src_id, dst_id, src_image, dst_image
            )

            edges[(src_id, dst_id)].add(dst_image)

    return edges


def dgl_multigraph(
    atoms: Atoms,
    neighbor_strategy: str = "k-nearest",
    cutoff: float = 8,
    max_neighbors: int = 12,
    atom_features: str = "atomic_number",
):
    """Get DGLGraph from atoms, go through pymatgen structure."""
    # go through pymatgen for neighbor API for now...
    try:
        structure = atoms.pymatgen_converter()
    except AttributeError:
        structure = atoms

    if neighbor_strategy == "k-nearest":
        edges = nearest_neighbor_edges(
            structure,
            cutoff=cutoff,
            max_neighbors=max_neighbors,
        )
    elif neighbor_strategy == "voronoi":
        edges = voronoi_edges(structure)

    u, v, r = build_undirected_edgedata(structure, edges)

    # build up atom attribute tensor
    species = [s.name for s in structure.species]
    node_features = torch.tensor(
        [_get_node_attributes(s, atom_features=atom_features) for s in species]
    ).type(torch.get_default_dtype())

    g = dgl.graph((u, v))
    g.ndata["atom_features"] = node_features
    g.edata["r"] = r

    return g


class Standardize(torch.nn.Module):
    """Standardize atom_features: subtract mean and divide by std."""

    def __init__(self, mean: torch.Tensor, std: torch.Tensor):
        """Register featurewise mean and standard deviation."""
        super().__init__()
        self.mean = mean
        self.std = std

    def forward(self, g: dgl.DGLGraph):
        """Apply standardization to atom_features."""
        g = g.local_var()
        h = g.ndata.pop("atom_features")
        g.ndata["atom_features"] = (h - self.mean) / self.std
        return g


class StructureDataset(torch.utils.data.Dataset):
    """Dataset of crystal DGLGraphs."""

    def __init__(
        self,
        structures,
        targets,
        ids=None,
        cutoff=8.0,
        maxrows=np.inf,
        atom_features="atomic_number",
        neighbor_strategy="k-nearest",
        transform=None,
    ):
        """Initialize the class."""
        self.graphs = []
        self.labels = []
        self.ids = []

        for idx, (structure, target, jid) in enumerate(
            tqdm(zip(structures, targets, ids))
        ):

            if idx >= maxrows:
                break

            a = Atoms.from_dict(structure)
            g = dgl_multigraph(
                a,
                atom_features=atom_features,
                neighbor_strategy=neighbor_strategy,
                cutoff=cutoff,
            )

            self.graphs.append(g)
            self.labels.append(target)
            self.ids.append(jid)

        self.labels = torch.tensor(self.labels).type(torch.get_default_dtype())
        self.transform = transform

    def __len__(self):
        """Get length."""
        return self.labels.shape[0]

    def __getitem__(self, idx):
        """Get StructureDataset sample."""
        g = self.graphs[idx]
        label = self.labels[idx]

        if self.transform:
            g = self.transform(g)

        return g, label

    def setup_standardizer(self):
        """Atom-wise feature standardization transform."""
        x = torch.cat([g.ndata["atom_features"] for g in self.graphs])
        self.atom_feature_mean = x.mean(0)
        self.atom_feature_std = x.std(0)

        self.transform = Standardize(
            self.atom_feature_mean, self.atom_feature_std
        )

    @staticmethod
    def collate(samples: List[Tuple[dgl.DGLGraph, torch.Tensor]]):
        """Dataloader helper to batch graphs cross `samples`."""
        graphs, labels = map(list, zip(*samples))
        batched_graph = dgl.batch(graphs)
        return batched_graph, torch.tensor(labels)


def get_train_val_loaders(
    dataset: str = "dft_3d",
    target: str = "formation_energy_peratom",
    atom_features: str = "atomic_number",
    neighbor_strategy: str = "k-nearest",
    n_train: int = 32,
    n_val: int = 32,
    n_test: int = 32,
    batch_size: int = 8,
    standardize: bool = False,
    split_seed=123,
):
    """Help function to set up Jarvis train and val dataloaders."""
    d = jdata(dataset)

    structures, targets, jv_ids = [], [], []
    for row in d:
        if row[target] != "na":
            structures.append(row["atoms"])
            targets.append(row[target])
            jv_ids.append(row["jid"])
    structures = np.array(structures)
    targets = np.array(targets)
    jv_ids = np.array(jv_ids)

    # shuffle consistently with https://github.com/txie-93/cgcnn/data.py
    # i.e. shuffle the index in place with standard library random.shuffle
    ids = np.arange(len(structures))
    random.seed(split_seed)
    random.shuffle(ids)

    id_train = ids[:n_train]
    id_val = ids[-(n_val + n_test) : -n_test]  # noqa:E203
    # id_test = ids[:-n_test]

    train_data = StructureDataset(
        structures[id_train],
        targets[id_train],
        ids=jv_ids[id_train],
        atom_features=atom_features,
        neighbor_strategy=neighbor_strategy,
    )
    if standardize:
        train_data.setup_standardizer()

    val_data = StructureDataset(
        structures[id_val],
        targets[id_val],
        ids=jv_ids[id_val],
        atom_features=atom_features,
        neighbor_strategy=neighbor_strategy,
        transform=train_data.transform,
    )

    # use a regular pytorch dataloader
    train_loader = DataLoader(
        train_data,
        batch_size=batch_size,
        shuffle=True,
        collate_fn=train_data.collate,
        drop_last=True,
    )

    val_loader = DataLoader(
        val_data,
        batch_size=batch_size,
        shuffle=False,
        collate_fn=val_data.collate,
        drop_last=True,
    )

    return train_loader, val_loader
