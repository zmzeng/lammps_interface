#!/usr/bin/env python
"""
Lammps interface main program. Lammps simulations are setup here.
"""
from memory_profiler import profile
import pickle
import os
import sys
import math
import re
import numpy as np
import networkx as nx
from . import ForceFields
import itertools
import operator
from .structure_data import from_CIF, write_CIF, clean
from .structure_data import write_RASPA_CIF, write_RASPA_sim_files, MDMC_config
from .CIFIO import CIF
from .ccdc import CCDC_BOND_ORDERS
from datetime import datetime
from .InputHandler import Options
from copy import deepcopy
from . import Molecules


if sys.version_info < (3, 0):
    input = raw_input


class LammpsSimulation(object):
    def __init__(self, options):
        self.name = clean(options.cif_file)
        self.special_commands = []
        self.options = options
        self.molecules = []
        self.subgraphs = []
        self.molecule_types = {}
        self.unique_atom_types = {}
        self.atom_ff_type = {}
        self.unique_bond_types = {}
        self.bond_ff_type = {}
        self.unique_angle_types = {}
        self.angle_ff_type = {}
        self.unique_dihedral_types = {}
        self.dihedral_ff_type = {}
        self.unique_improper_types = {}
        self.improper_ff_type = {}
        self.unique_pair_types = {}
        self.pair_in_data = True
        self.separate_molecule_types = True
        self.framework = True # Flag if a framework exists in the simulation.
        self.supercell = (1, 1, 1) # keep track of supercell size
        self.type_molecules = {}
        self.no_molecule_pair = True  # ensure that h-bonding will not occur between molecules of the same type
        self.fix_shake = {}
        self.fix_rigid = {}

    def set_MDMC_config(self, MDMC_config):
        self.MDMC_config = MDMC_config

    def unique_atoms(self, g):
        """Computes the number of unique atoms in the structure"""
        count = len(self.unique_atom_types)
        fwk_nodes = sorted(g.nodes())
        molecule_nodes = []
        # check if this is the main graph
        if g == self.graph:
            for k in sorted(self.molecule_types.keys()):
                nds = []
                for m in self.molecule_types[k]:

                    jnodes = sorted(nx.read_gpickle(self.subgraphs[m]).nodes())
                    nds += jnodes

                    for n in jnodes:
                        del fwk_nodes[fwk_nodes.index(n)]
                molecule_nodes.append(nds)
            molecule_nodes.append(fwk_nodes)

        # determine if the graph is the main structure, or a molecule template
        # this is *probably* not the best way to do it.
        moltemplate = ("Molecules" in "%s"%g.__class__)
        mainstructr = ("structure_data" in "%s"%g.__class__)
        if (moltemplate and mainstructr):
            print("ERROR: there is some confusion about class assignment with "+
                  "MolecularGraphs.  You should probably contact one of the developers.")
            sys.exit()

        for node, data in g.nodes_iter2(data=True):
            if self.separate_molecule_types and molecule_nodes and mainstructr:
                molid = [j for j,mol in enumerate(molecule_nodes) if node in mol]
                molid = molid[0]
            elif moltemplate:
                # random keyboard mashing. Just need to separate this from other atom types in the
                # system. This is important when defining separating the Molecule's atom types
                # from other atom types in the framework that would otherwise be identical.
                # This allows for the construction of the molecule group for this template.
                molid = 23523523
            else:
                molid = 0
            # add factor for h_bond donors
            if data['force_field_type'] is None:
                if data['h_bond_donor']:
                    # add neighbors to signify type of hbond donor
                    label = (data['element'], data['h_bond_donor'], molid, tuple(sorted([g.node[j]['element'] for j in g.neighbors(node)])))
                else:
                    label = (data['element'], data['h_bond_donor'], molid)
            else:
                if data['h_bond_donor']:
                    # add neighbors to signify type of hbond donor
                    label = (data['force_field_type'], data['h_bond_donor'], molid, tuple(sorted([g.node[j]['element'] for j in g.neighbors(node)])))
                else:
                    label = (data['force_field_type'], data['h_bond_donor'], molid)

            try:
                type = self.atom_ff_type[label]
            except KeyError:
                count += 1
                type = count
                self.atom_ff_type[label] = type
                self.unique_atom_types[type] = (node, data)
                if not moltemplate:
                    self.type_molecules[type] = molid
            data['ff_type_index'] = type

    def unique_bonds(self, g):
        """Computes the number of unique bonds in the structure"""
        count = len(self.unique_bond_types)
        for n1, n2, data in g.edges_iter2(data=True):
            btype = "%s"%data['potential']

            try:
                type = self.bond_ff_type[btype]

            except KeyError:
                try:
                    if data['potential'].special_flag == 'shake':
                        self.fix_shake.setdefault('bonds', []).append(count+1)
                except AttributeError:
                    pass
                count += 1
                type = count
                self.bond_ff_type[btype] = type

                self.unique_bond_types[type] = (n1, n2, data)

            data['ff_type_index'] = type

    def unique_angles(self, g):
        count = len(self.unique_angle_types)
        for b, data in g.nodes_iter2(data=True):
            # compute and store angle terms
            try:
                ang_data = data['angles']

                for (a, c), val in ang_data.items():
                    atype = "%s"%val['potential']
                    try:
                        type = self.angle_ff_type[atype]

                    except KeyError:
                        count += 1
                        try:
                            if val['potential'].special_flag == 'shake':
                                self.fix_shake.setdefault('angles', []).append(count)
                        except AttributeError:
                            pass
                        type = count
                        self.angle_ff_type[atype] = type
                        self.unique_angle_types[type] = (a, b, c, val)
                    val['ff_type_index'] = type
                    # update original dictionary
                    data['angles'][(a, c)] = val
            except KeyError:
                # no angle associated with this node.
                pass

    def unique_dihedrals(self, g):
        count = len(self.unique_dihedral_types)
        dihedral_type = {}
        for b, c, data in g.edges_iter2(data=True):
            try:
                dihed_data = data['dihedrals']
                for (a, d), val in dihed_data.items():
                    dtype = "%s"%val['potential']
                    try:
                        type = dihedral_type[dtype]
                    except KeyError:
                        count += 1
                        type = count
                        dihedral_type[dtype] = type
                        self.unique_dihedral_types[type] = (a, b, c, d, val)
                    val['ff_type_index'] = type
                    # update original dictionary
                    data['dihedrals'][(a,d)] = val
            except KeyError:
                # no dihedrals associated with this edge
                pass

    def unique_impropers(self, g):
        count = len(self.unique_improper_types)

        for b, data in g.nodes_iter2(data=True):
            try:
                rem = []
                imp_data = data['impropers']
                for (a, c, d), val in imp_data.items():
                    if val['potential'] is not None:
                        itype = "%s"%val['potential']
                        try:
                            type = self.improper_ff_type[itype]
                        except KeyError:
                            count += 1
                            type = count
                            self.improper_ff_type[itype] = type
                            self.unique_improper_types[type] = (a, b, c, d, val)

                        val['ff_type_index'] = type
                    else:
                        rem.append((a,c,d))

                for m in rem:
                    data['impropers'].pop(m)

            except KeyError:
                # no improper terms associated with this atom
                pass

    def unique_pair_terms(self):
        pot_names = []
        nodes_list = sorted(self.unique_atom_types.keys())
        electro_neg_atoms = ["N", "O", "F"]
        for n, data in self.graph.nodes_iter2(data=True):
            if data['h_bond_donor']:
                pot_names.append('h_bonding')
            if data['tabulated_potential']:
                pot_names.append('table')
            pot_names.append(data['pair_potential'].name)
        # mix yourself

        table_str = ""
        if len(list(set(pot_names))) > 1 or (any(['buck' in i for i in list(set(pot_names))])):
            self.pair_in_data = False
            for (i, j) in itertools.combinations_with_replacement(nodes_list, 2):
                (n1, i_data), (n2, j_data) = self.unique_atom_types[i], self.unique_atom_types[j]
                mol1 = self.type_molecules[i]
                mol2 = self.type_molecules[j]
                # test to see if h-bonding to occur between molecules
                pairwise_test = ((mol1 != mol2 and self.no_molecule_pair) or (not self.no_molecule_pair))
                if i_data['tabulated_potential'] and j_data['tabulated_potential']:
                    table_pot = deepcopy(i_data)
                    table_str += table_pot['table_function'](i_data,j_data, table_pot)
                    table_pot['table_potential'].filename = "table." + self.name
                    self.unique_pair_types[(i, j, 'table')] = table_pot

                if (i_data['h_bond_donor'] and j_data['element'] in electro_neg_atoms and pairwise_test and not j_data['h_bond_donor']):
                    hdata = deepcopy(i_data)
                    hdata['h_bond_potential'] = hdata['h_bond_function'](n2, self.graph, flipped=False)
                    hdata['tabulated_potential'] = False
                    self.unique_pair_types[(i,j,'hb')] = hdata
                if (j_data['h_bond_donor'] and i_data['element'] in electro_neg_atoms and pairwise_test and not i_data['h_bond_donor']):
                    hdata = deepcopy(j_data)
                    hdata['tabulated_potential'] = False
                    hdata['h_bond_potential'] = hdata['h_bond_function'](n1, self.graph, flipped=True)
                    self.unique_pair_types[(i,j,'hb')] = hdata
                # mix Lorentz-Berthelot rules
                pair_data = deepcopy(i_data)
                if 'buck' in i_data['pair_potential'].name and 'buck' in j_data['pair_potential'].name:
                    eps1 = i_data['pair_potential'].eps
                    eps2 = j_data['pair_potential'].eps
                    sig1 = i_data['pair_potential'].sig
                    sig2 = j_data['pair_potential'].sig
                    eps = np.sqrt(eps1*eps2)
                    Rv = (sig1 + sig2)
                    Rho = Rv/12.0
                    A = 1.84e5 * eps
                    C=2.25*(Rv)**6*eps

                    pair_data['pair_potential'].A = A
                    pair_data['pair_potential'].rho = Rho
                    pair_data['pair_potential'].C = C
                    pair_data['tabulated_potential'] = False
                    # assuming i_data has the same pair_potential name as j_data
                    self.unique_pair_types[(i,j, i_data['pair_potential'].name)] = pair_data
                elif 'lj' in i_data['pair_potential'].name and 'lj' in j_data['pair_potential'].name:

                    pair_data['pair_potential'].eps = np.sqrt(i_data['pair_potential'].eps*j_data['pair_potential'].eps)
                    pair_data['pair_potential'].sig = (i_data['pair_potential'].sig + j_data['pair_potential'].sig)/2.
                    pair_data['tabulated_potential'] = False
                    self.unique_pair_types[(i,j, i_data['pair_potential'].name)] = pair_data

        # can be mixed by lammps
        else:
            for b in sorted(list(self.unique_atom_types.keys())):
                data = self.unique_atom_types[b][1]
                pot = data['pair_potential']
                self.unique_pair_types[b] = data

        if (table_str):
            f = open('table.'+self.name, 'w')
            f.writelines(table_str)
            f.close()
        return

    def define_styles(self):
        # should be more robust, some of the styles require multiple parameters specified on these lines
        self.kspace_style = "ewald %f"%(0.000001)
        bonds = set([j['potential'].name for n1, n2, j in list(self.unique_bond_types.values())])
        if len(list(bonds)) > 1:
            self.bond_style = "hybrid %s"%" ".join(list(bonds))
        elif len(list(bonds)) == 1:
            self.bond_style = "%s"%list(bonds)[0]
            for n1, n2, b in list(self.unique_bond_types.values()):
                b['potential'].reduced = True
        else:
            self.bond_style = ""
        angles = set([j['potential'].name for a,b,c,j in list(self.unique_angle_types.values())])
        if len(list(angles)) > 1:
            self.angle_style = "hybrid %s"%" ".join(list(angles))
        elif len(list(angles)) == 1:
            self.angle_style = "%s"%list(angles)[0]
            for a,b,c,ang in list(self.unique_angle_types.values()):
                ang['potential'].reduced = True
                if (ang['potential'].name == "class2"):
                    ang['potential'].bb.reduced=True
                    ang['potential'].ba.reduced=True
        else:
            self.angle_style = ""

        dihedrals = set([j['potential'].name for a,b,c,d,j in list(self.unique_dihedral_types.values())])
        if len(list(dihedrals)) > 1:
            self.dihedral_style = "hybrid %s"%" ".join(list(dihedrals))
        elif len(list(dihedrals)) == 1:
            self.dihedral_style = "%s"%list(dihedrals)[0]
            for a,b,c,d, di in list(self.unique_dihedral_types.values()):
                di['potential'].reduced = True
                if (di['potential'].name == "class2"):
                    di['potential'].mbt.reduced=True
                    di['potential'].ebt.reduced=True
                    di['potential'].at.reduced=True
                    di['potential'].aat.reduced=True
                    di['potential'].bb13.reduced=True
        else:
            self.dihedral_style = ""

        impropers = set([j['potential'].name for a,b,c,d,j in list(self.unique_improper_types.values())])
        if len(list(impropers)) > 1:
            self.improper_style = "hybrid %s"%" ".join(list(impropers))
        elif len(list(impropers)) == 1:
            self.improper_style = "%s"%list(impropers)[0]
            for a,b,c,d,i in list(self.unique_improper_types.values()):
                i['potential'].reduced = True
                if (i['potential'].name == "class2"):
                    i['potential'].aa.reduced=True
        else:
            self.improper_style = ""
        pairs = set(["%r"%(j['pair_potential']) for j in list(self.unique_pair_types.values())]) | \
                set(["%r"%(j['h_bond_potential']) for j in list(self.unique_pair_types.values()) if j['h_bond_potential'] is not None]) | \
                set(["%r"%(j['table_potential']) for j in list(self.unique_pair_types.values()) if j['tabulated_potential']])
        if len(list(pairs)) > 1:
            self.pair_style = "hybrid/overlay %s"%(" ".join(list(pairs)))
        else:
            self.pair_style = "%s"%list(pairs)[0]
            for p in list(self.unique_pair_types.values()):
                p['pair_potential'].reduced = True

    def set_graph(self, graph):
        self.graph = graph

        try:
            if(not self.options.force_field == "UFF") and (not self.options.force_field == "Dreiding") and \
                    (not self.options.force_field == "UFF4MOF"):
                self.graph.find_metal_sbus = True # true for BTW_FF and Dubbeldam
            if (self.options.force_field == "Dubbeldam"):
                self.graph.find_organic_sbus = True

            self.graph.compute_topology_information(self.cell, self.options.tol, self.options.neighbour_size)
        except AttributeError:
            # no cell set yet
            pass

    def set_cell(self, cell):
        self.cell = cell
        try:
            self.graph.compute_topology_information(self.cell, self.options.tol, self.options.neighbour_size)
        except AttributeError:
            # no graph set yet
            pass

    @profile
    def split_graph(self):

        self.compute_molecules()
        if (self.molecules):
            print("Molecules found in the framework, separating.")
            molid=0
            for molecule in self.molecules:
                molid += 1
                sg = self.cut_molecule(molecule)
                sg.molecule_id = molid
                # unwrap coordinates
                sg.unwrap_node_coordinates(self.cell)
                file2store = "sg"+str(molid)+'.bz2'
                self.subgraphs.append(file2store)
                nx.write_gpickle(sg, file2store)
        type = 0
        temp_types = {}
        for i, j in itertools.combinations(range(len(self.subgraphs)), 2):
            if nx.read_gpickle(self.subgraphs[i]).number_of_nodes() != nx.read_gpickle(self.subgraphs[j]).number_of_nodes():
                continue

            #TODO(pboyd): For complex 'floppy' molecules, a rigid 3D clique detection
            # algorithm won't work very well. Inchi or smiles comparison may be better,
            # but that would require using openbabel. I'm trying to keep this
            # code as independent of non-standard python libraries as possible.
            matched = nx.read_gpickle(self.subgraphs[i]) | nx.read_gpickle(self.subgraphs[j])
            if (len(matched) == nx.read_gpickle(self.subgraphs[i]).number_of_nodes()):
                if i not in list(temp_types.keys()) and j not in list(temp_types.keys()):
                    type += 1
                    temp_types[i] = type
                    temp_types[j] = type
                    self.molecule_types.setdefault(type, []).append(i)
                    self.molecule_types[type].append(j)
                else:
                    try:
                        type = temp_types[i]
                        temp_types[j] = type
                    except KeyError:
                        type = temp_types[j]
                        temp_types[i] = type
                    if i not in self.molecule_types[type]:
                        self.molecule_types[type].append(i)
                    if j not in self.molecule_types[type]:
                        self.molecule_types[type].append(j)
        unassigned = set(range(len(self.subgraphs))) - set(list(temp_types.keys()))
        for j in list(unassigned):
            type += 1
            self.molecule_types[type] = [j]

    def assign_force_fields(self):

        attr = {'graph':self.graph, 'cutoff':self.options.cutoff, 'h_bonding':self.options.h_bonding,
                'keep_metal_geometry':self.options.fix_metal, 'bondtype':self.options.dreid_bond_type}
        param = getattr(ForceFields, self.options.force_field)(**attr)

        self.special_commands += param.special_commands()

        # apply different force fields.
        for mtype in list(self.molecule_types.keys()):
            # prompt for ForceField?
            rep = nx.read_gpickle(self.subgraphs[self.molecule_types[mtype][0]])
            #response = input("Would you like to apply a new force field to molecule type %i with atoms (%s)? [y/n]: "%
            #        (mtype, ", ".join([rep.node[j]['element'] for j in rep.nodes()])))
            #ff = self.options.force_field
            #if response.lower() in ['y','yes']:
            #    ff = input("Please enter the name of the force field: ")
            #elif response.lower() in ['n', 'no']:
            #    pass
            #else:
            #    print("Unrecognized command: %s"%response)

            ff = self.options.mol_ff
            if ff is None:
                ff = self.options.force_field
                atoms = ", ".join([rep.node[j]['element'] for j in rep.nodes()])
                print("WARNING: Molecule %s with atoms (%s) will be using the %s force field as no "%(mtype,atoms,ff)+
                      " value was set for molecules. To prevent this warning "+
                      "set --molecule-ff=[some force field] on the command line.")
            h_bonding = False
            if (ff == "Dreiding"):
                hbonding = input("Would you like this molecule type to have hydrogen donor potentials? [y/n]: ")
                if hbonding.lower() in ['y', 'yes']:
                    h_bonding = True
                elif hbonding.lower() in ['n', 'no']:
                    h_bonding = False
                else:
                    print("Unrecognized command: %s"%hbonding)
                    sys.exit()
            for m in self.molecule_types[mtype]:
                # Water check
                # currently only works if the cif file contains water particles without dummy atoms.
                ngraph = nx.read_gpickle(self.subgraphs[m])
                self.assign_molecule_ids(ngraph)
                mff = ff
                if ff[-5:] == "Water":
                    self.add_water_model(ngraph, ff)
                    mff = mff[:-6] # remove _Water from end of name
                if ff[-3:] == "CO2":
                    self.add_co2_model(ngraph, ff)
                p = getattr(ForceFields, mff)(graph=nx.read_gpickle(self.subgraphs[m]),
                                         cutoff=self.options.cutoff,
                                         h_bonding=h_bonding)
                self.special_commands += p.special_commands()

    def assign_molecule_ids(self, graph):
        for node in graph.nodes():
            graph.node[node]['molid'] = graph.molecule_id

    def molecule_template(self, mol):
        """ Construct a molecule template for
        reading and insertions in a LAMMPS simulation.

        This combines two classes which have
        been separated conceptually - ForceField and
        Molecules.
        For some molecules, the force field is implicit
        within the structure (e.g. TIP5P_Water molecule
        must be used with the TIP5P ForceField).
        But one can imagine cases where this is not true
        (alkanes? CO2?).

        """
        # no error checking here, it is assumed that the user
        # knows which force field to pair with which molecule
        # I'm not sure what would happen if there were a mismatch
        # but hopefully error-checking elsewhere in the code
        # will catch these things.
        molecule = getattr(Molecules, mol)()
        if self.options.mol_ff is None:
            mol_ff = self.options.force_field

        elif self.options.mol_ff.endswith("_Water"):
            # parse if _Water is at the end to get the force
            # fields for various water models.
            mol_ff = mol[:-6]
        else:
            # just take the general force field used on the
            # framework
            mol_ff = self.options.mol_ff
        #TODO(pboyd): Check how h-bonding is handeled at this level
        ff = getattr(ForceFields, mol_ff)(graph=molecule,
                                     cutoff=self.options.cutoff)

        # add the unique potentials to the unique_dictionaries.
        self.unique_atoms(molecule)
        self.unique_bonds(molecule)
        self.unique_angles(molecule)
        self.unique_dihedrals(molecule)
        self.unique_impropers(molecule)
        # somehow update atom, bond, angle, dihedral, improper etc. types to
        # include atomic species that don't exist yet..
        self.template_molecule = molecule
        template_file = "%s.molecule"%molecule.__class__.__name__
        file = open(template_file, 'w')
        file.writelines(molecule.str(atom_types=self.atom_ff_type))
        file.close()
        print('Molecule template file written as %s'%template_file)

    def add_co2_model(self, ngraph, ff):
        size = ngraph.number_of_nodes()
        if size < 3 or size > 3:
            print("Error: cannot assign %s "%(ff) +
                  "to molecule of size %i, with "%(size)+
                  "atoms (%s)"%(", ".join([ngraph.node[kk]['element'] for
                                           kk in ngraph.nodes()])))
            print("If this is a CO2 molecule with pre-existing "+
                    "dummy atoms for a particular force field, "+
                    "please remove them and re-run this code.")
            sys.exit()
        for node in ngraph.nodes():
            if ngraph.node[node]['element'] == "C":
                catom = ngraph.node[node]
            elif ngraph.node[node]['element'] == "O":
                try:
                    oatom1
                    o2id = node
                    oatom2 = ngraph.node[node]
                except NameError:
                    o1id = node
                    oatom1 = ngraph.node[node]

        co2 = getattr(Molecules, ff)()
        co2.approximate_positions(C_pos  = catom['cartesian_coordinates'],
                                  O_pos1 = oatom1['cartesian_coordinates'],
                                  O_pos2 = oatom2['cartesian_coordinates'])

        # update the co2 atoms in the graph with the force field molecule
        mol_c = deepcopy(co2.node[1])
        mol_o1 = deepcopy(co2.node[2])
        mol_o2 = deepcopy(co2.node[3])
        # hackjob - get rid of the angle data on the carbon, so that
        # the framework indexed values for each oxygen remain with the carbon atom.
        mol_c.pop('angles')
        catom.update(mol_c)
        oatom1.update(mol_o1)
        oatom2.update(mol_o2)
        #for node in ngraph.nodes():
        #    #data = deepcopy(ngraph.node[node]) # doesn't work - some of the data is
        #                                        # specific to the molecule in the
        #                                        # framework.

        #    if data['element'] == "C":
        #        cid = node
        #        ngraph.node[node] = co2.node[1].copy()
        #    elif data['element'] == "O":
        #        try:
        #            otm1
        #            ngraph.node[node] = co2.node[3].copy()
        #        except NameError:
        #            otm1 = node
        #            ngraph.node[node] = co2.node[2].copy()

    def add_water_model(self, ngraph, ff):
        size = ngraph.number_of_nodes()
        if size < 3 or size > 3:
            print("Error: cannot assign %s "%(ff) +
                  "to molecule of size %i, with "%(size)+
                  "atoms (%s)"%(", ".join([ngraph.node[kk]['element'] for
                                           kk in ngraph.nodes()])))
            print("If this is a water molecule with pre-existing "+
                    "dummy atoms for a particular force field, "+
                    "please remove them and re-run this code.")
            sys.exit()
        for node in ngraph.nodes():
            if ngraph.node[node]['element'] == "O":
                oid = node
                oatom = ngraph.node[node]
            elif ngraph.node[node]['element'] == "H":
                try:
                    hatom1
                    h2id = node
                    hatom2 = ngraph.node[node]
                except NameError:
                    h1id = node
                    hatom1 = ngraph.node[node]

        h2o = getattr(Molecules, ff)()
        h2o.approximate_positions(O_pos  = oatom['cartesian_coordinates'],
                                  H_pos1 = hatom1['cartesian_coordinates'],
                                  H_pos2 = hatom2['cartesian_coordinates'])

        # update the water atoms in the graph with the force field molecule
        mol_o = deepcopy(h2o.node[1])
        mol_h1 = deepcopy(h2o.node[2])
        mol_h2 = deepcopy(h2o.node[3])
        # hackjob - get rid of the angle data on the carbon, so that
        # the framework indexed values for each oxygen remain with the carbon atom.
        try:
            mol_o.pop('angles')
        except KeyError:
            pass

        oatom.update(mol_o)
        hatom1.update(mol_h1)
        hatom2.update(mol_h2)
        # update the water atoms in the graph with the force field molecule
        #for node in ngraph.nodes():
        #    data = deepcopy(ngraph.node[node])
        #    if data['element'] == "O":
        #        oid = node
        #        ngraph.node[node] = h2o.node[1].copy()
        #    elif data['element'] == "H":
        #        try:
        #            htm1
        #            ngraph.node[node] = h2o.node[3].copy()
        #        except NameError:
        #            htm1 = node
        #            ngraph.node[node] = h2o.node[2].copy()

        # add dummy particles
        for dx in h2o.nodes():
            if dx > 3:
                self.increment_graph_sizes()
                os = ngraph.original_size
                ngraph.add_node(os, **h2o.node[dx])
                ngraph.add_edge(oid, os, order=1.,
                                weight=1.,
                                length=h2o.Rdum,
                                symflag='1_555',
                                )
                ngraph.sorted_edge_dict.update({(oid, os): (oid, os)})
                ngraph.sorted_edge_dict.update({(os, oid): (oid, os)})
        # compute new angles between dummy atoms
        ngraph.compute_angles()


    def increment_graph_sizes(self, inc=1):
        self.graph.original_size += inc
        for mtype in list(self.molecule_types.keys()):
            for m in self.molecule_types[mtype]:
                graph = nx.read_gpickle(self.subgraphs[m])
                graph.original_size += 1

    def compute_simulation_size(self):

        if self.options.orthogonalize:
            if not (np.allclose(self.cell.alpha, 90., atol=1) and np.allclose(self.cell.beta, 90., atol=1) and\
                    np.allclose(self.cell.gamma, 90., atol=1)):

                print("WARNING: Orthogonalization of simulation cell requested. This can "+
                      "make simulation sizes incredibly large. I hope you know, what you "+
                      "are doing!")
                transformation_matrix = self.cell.orthogonal_transformation()
                self.graph.redefine_lattice(transformation_matrix, self.cell)
        supercell = self.cell.minimum_supercell(self.options.cutoff)
        if np.any(np.array(supercell) > 1):
            print("WARNING: unit cell is not large enough to"
                  +" support a non-bonded cutoff of %.2f Angstroms."%self.options.cutoff)

        if(self.options.replication is not None):
            supercell = tuple(map(int, re.split('x| |, |,',self.options.replication)))
            if(len(supercell) != 3):
                if(supercell[0] < 1 or supercell[1] < 1 or supercell[2] < 1):
                    print("Incorrect supercell requested: %s\n"%(supercell))
                    print("Use <ixjxk> format")
                    print("Exiting...")
                    sys.exit()
        self.supercell=supercell
        if np.any(np.array(supercell) > 1):
            print("Re-sizing to a %i x %i x %i supercell. "%(supercell))

            #TODO(pboyd): apply to subgraphs as well, if requested.
            self.graph.build_supercell(supercell, self.cell)
            molcount = 0
            if self.subgraphs:
                molcount = max([nx.read_gpickle(g).molecule_id for g in self.subgraphs])

            for mtype in list(self.molecule_types.keys()):
                # prompt for replication of this molecule in the supercell.
                rep = nx.read_gpickle(self.subgraphs[self.molecule_types[mtype][0]])
                response = input("Would you like to replicate molceule %i with atoms (%s) in the supercell? [y/n]: "%
                        (mtype, ", ".join([rep.node[j]['element'] for j in rep.nodes()])))
                if response in ['y', 'Y', 'yes']:
                    sg = nx.read_gpickle(self.subgraphs[m])
                    for m in self.molecule_types[mtype]:
                        sg.build_supercell(supercell, self.cell, track_molecule=True, molecule_len=molcount)
                    nx.write_gpickle(sg, self.subgraphs[m])
            self.cell.update_supercell(supercell)

    def merge_graphs(self):
        for mgraph in self.subgraphs:
            self.graph += nx.read_gpickle(mgraph)
        for node in self.graph.nodes():
            data=self.graph.node[node]
        if sorted(self.graph.nodes()) != [i+1 for i in range(len(self.graph.nodes()))]:
            print("Re-labelling atom indices.")
            reorder_dic = {i:j+1 for (i, j) in zip(sorted(self.graph.nodes()), range(len(self.graph.nodes())))}
            self.graph.reorder_labels(reorder_dic)
            for mgraph in self.subgraphs:
                sg = nx.read_gpickle(mgraph)
                sg.reorder_labels(reorder_dic)
                nx.write_gpickle(sg, mgraph)

    def write_lammps_files(self, wd=None):
        self.unique_atoms(self.graph)
        self.unique_bonds(self.graph)
        self.unique_angles(self.graph)
        self.unique_dihedrals(self.graph)
        self.unique_impropers(self.graph)
        if self.options.insert_molecule:
            self.molecule_template(self.options.insert_molecule)
        self.unique_pair_terms()
        self.define_styles()

        if wd is None:
            wd = os.getcwd()

        data_str = self.construct_data_file()
        with open(os.path.join(wd, "data.%s" % self.name), 'w') as datafile:
            datafile.writelines(data_str)

        inp_str = self.construct_input_file()
        with open(os.path.join(wd, "in.%s" % self.name), 'w') as inpfile:
            inpfile.writelines(inp_str)

        print("Files created! -> %s" % wd)

    def construct_data_file(self):

        t = datetime.today()
        string = "Created on %s\n\n"%t.strftime("%a %b %d %H:%M:%S %Y %Z")

        if(len(self.unique_atom_types.keys()) > 0):
            string += "%12i atoms\n"%(nx.number_of_nodes(self.graph))
        if(len(self.unique_bond_types.keys()) > 0):
            string += "%12i bonds\n"%(nx.number_of_edges(self.graph))
        if(len(self.unique_angle_types.keys()) > 0):
            string += "%12i angles\n"%(self.graph.count_angles())
        if(len(self.unique_dihedral_types.keys()) > 0):
            string += "%12i dihedrals\n"%(self.graph.count_dihedrals())
        if (len(self.unique_improper_types.keys()) > 0):
            string += "%12i impropers\n"%(self.graph.count_impropers())

        if(len(self.unique_atom_types.keys()) > 0):
            string += "\n%12i atom types\n"%(len(self.unique_atom_types.keys()))
        if(len(self.unique_bond_types.keys()) > 0):
            string += "%12i bond types\n"%(len(self.unique_bond_types.keys()))
        if(len(self.unique_angle_types.keys()) > 0):
            string += "%12i angle types\n"%(len(self.unique_angle_types.keys()))
        if(len(self.unique_dihedral_types.keys()) > 0):
            string += "%12i dihedral types\n"%(len(self.unique_dihedral_types.keys()))
        if (len(self.unique_improper_types.keys()) > 0):
            string += "%12i improper types\n"%(len(self.unique_improper_types.keys()))

        string += "%19.6f %10.6f %s %s\n"%(0., self.cell.lx, "xlo", "xhi")
        string += "%19.6f %10.6f %s %s\n"%(0., self.cell.ly, "ylo", "yhi")
        string += "%19.6f %10.6f %s %s\n"%(0., self.cell.lz, "zlo", "zhi")
        #if not (np.allclose(np.array([self.cell.xy, self.cell.xz, self.cell.yz]), 0.0)):
        #    string += "%19.6f %10.6f %10.6f %s %s %s\n"%(self.cell.xy, self.cell.xz, self.cell.yz, "xy", "xz", "yz")
        string += "%19.6f %10.6f %10.6f %s %s %s\n"%(self.cell.xy,
                                                     self.cell.xz,
                                                     self.cell.yz,
                                                     "xy", "xz", "yz")

        # Let's track the forcefield potentials that haven't been calc'd or user specified
        no_bond = []
        no_angle = []
        no_dihedral = []
        no_improper = []

        # this should be non-zero, but just in case..
        if(len(self.unique_atom_types.keys()) > 0):
            string += "\nMasses\n\n"
            for key in sorted(self.unique_atom_types.keys()):
                unq_atom = self.unique_atom_types[key][1]
                mass, type = unq_atom['mass'], unq_atom['force_field_type']
                string += "%5i %15.9f # %s\n"%(key, mass, type)

        if(len(self.unique_bond_types.keys()) > 0):
            string += "\nBond Coeffs\n\n"
            for key in sorted(self.unique_bond_types.keys()):
                n1, n2, bond = self.unique_bond_types[key]
                atom1, atom2 = self.graph.node[n1], self.graph.node[n2]
                if bond['potential'] is None:
                    no_bond.append("%5i : %s %s"%(key,
                                                  atom1['force_field_type'],
                                                  atom2['force_field_type']))
                else:
                    ff1, ff2 = (atom1['force_field_type'],
                                atom2['force_field_type'])

                    string += "%5i %s "%(key, bond['potential'])
                    string += "# %s %s\n"%(ff1, ff2)

        class2angle = False
        if(len(self.unique_angle_types.keys()) > 0):
            string += "\nAngle Coeffs\n\n"
            for key in sorted(self.unique_angle_types.keys()):
                a, b, c, angle = self.unique_angle_types[key]
                atom_a, atom_b, atom_c = self.graph.node[a], \
                                         self.graph.node[b], \
                                         self.graph.node[c]

                if angle['potential'] is None:
                    no_angle.append("%5i : %s %s %s"%(key,
                                          atom_a['force_field_type'],
                                          atom_b['force_field_type'],
                                          atom_c['force_field_type']))
                else:
                    if (angle['potential'].name == "class2"):
                        class2angle = True

                    string += "%5i %s "%(key, angle['potential'])
                    string += "# %s %s %s\n"%(atom_a['force_field_type'],
                                              atom_b['force_field_type'],
                                              atom_c['force_field_type'])

        if(class2angle):
            string += "\nBondBond Coeffs\n\n"
            for key in sorted(self.unique_angle_types.keys()):
                a, b, c, angle = self.unique_angle_types[key]
                atom_a, atom_b, atom_c = self.graph.node[a], \
                                         self.graph.node[b], \
                                         self.graph.node[c]
                if (angle['potential'].name!="class2"):
                    string += "%5i skip "%(key)
                    string += "# %s %s %s\n"%(atom_a['force_field_type'],
                                              atom_b['force_field_type'],
                                              atom_c['force_field_type'])
                else:
                    try:
                        string += "%5i %s "%(key, angle['potential'].bb)
                        string += "# %s %s %s\n"%(atom_a['force_field_type'],
                                                  atom_b['force_field_type'],
                                                  atom_c['force_field_type'])
                    except AttributeError:
                        pass

            string += "\nBondAngle Coeffs\n\n"
            for key in sorted(self.unique_angle_types.keys()):
                a, b, c, angle = self.unique_angle_types[key]
                atom_a, atom_b, atom_c = self.graph.node[a],\
                                         self.graph.node[b],\
                                         self.graph.node[c]
                if (angle['potential'].name!="class2"):
                    string += "%5i skip  "%(key)
                    string += "# %s %s %s\n"%(atom_a['force_field_type'],
                                              atom_b['force_field_type'],
                                              atom_c['force_field_type'])
                else:
                    try:
                        string += "%5i %s "%(key, angle['potential'].ba)
                        string += "# %s %s %s\n"%(atom_a['force_field_type'],
                                                  atom_b['force_field_type'],
                                                  atom_c['force_field_type'])
                    except AttributeError:
                        pass

        class2dihed = False
        if(len(self.unique_dihedral_types.keys()) > 0):
            string +=  "\nDihedral Coeffs\n\n"
            for key in sorted(self.unique_dihedral_types.keys()):
                a, b, c, d, dihedral = self.unique_dihedral_types[key]
                atom_a, atom_b, atom_c, atom_d = self.graph.node[a], \
                                                 self.graph.node[b], \
                                                 self.graph.node[c], \
                                                 self.graph.node[d]
                if dihedral['potential'] is None:
                    no_dihedral.append("%5i : %s %s %s %s"%(key,
                                       atom_a['force_field_type'],
                                       atom_b['force_field_type'],
                                       atom_c['force_field_type'],
                                       atom_d['force_field_type']))
                else:
                    if(dihedral['potential'].name == "class2"):
                        class2dihed = True
                    string += "%5i %s "%(key, dihedral['potential'])
                    string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                                 atom_b['force_field_type'],
                                                 atom_c['force_field_type'],
                                                 atom_d['force_field_type'])

        if (class2dihed):
            string += "\nMiddleBondTorsion Coeffs\n\n"
            for key in sorted(self.unique_dihedral_types.keys()):
                a, b, c, d, dihedral = self.unique_dihedral_types[key]
                atom_a, atom_b, atom_c, atom_d = self.graph.node[a], \
                                                 self.graph.node[b], \
                                                 self.graph.node[c], \
                                                 self.graph.node[d]

                if (dihedral['potential'].name!="class2"):
                    string += "%5i skip "%(key)
                    string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                              atom_b['force_field_type'],
                                              atom_c['force_field_type'],
                                              atom_d['force_field_type'])
                else:
                    try:
                        string += "%5i %s "%(key, dihedral['potential'].mbt)
                        string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                                  atom_b['force_field_type'],
                                                  atom_c['force_field_type'],
                                                  atom_d['force_field_type'])
                    except AttributeError:
                        pass
            string += "\nEndBondTorsion Coeffs\n\n"
            for key in sorted(self.unique_dihedral_types.keys()):
                a, b, c, d, dihedral = self.unique_dihedral_types[key]
                atom_a, atom_b, atom_c, atom_d = self.graph.node[a], \
                                                 self.graph.node[b], \
                                                 self.graph.node[c], \
                                                 self.graph.node[d]
                if (dihedral['potential'].name!="class2"):
                    string += "%5i skip "%(key)
                    string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                              atom_b['force_field_type'],
                                              atom_c['force_field_type'],
                                              atom_d['force_field_type'])
                else:
                    try:
                        string += "%5i %s "%(key, dihedral['potential'].ebt)
                        string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                                  atom_b['force_field_type'],
                                                  atom_c['force_field_type'],
                                                  atom_d['force_field_type'])
                    except AttributeError:
                        pass
            string += "\nAngleTorsion Coeffs\n\n"
            for key in sorted(self.unique_dihedral_types.keys()):
                a, b, c, d, dihedral = self.unique_dihedral_types[key]
                atom_a, atom_b, atom_c, atom_d = self.graph.node[a], \
                                                 self.graph.node[b], \
                                                 self.graph.node[c], \
                                                 self.graph.node[d]
                if (dihedral['potential'].name!="class2"):
                    string += "%5i skip "%(key)
                    string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                              atom_b['force_field_type'],
                                              atom_c['force_field_type'],
                                              atom_d['force_field_type'])
                else:
                    try:
                        string += "%5i %s "%(key, dihedral['potential'].at)
                        string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                                  atom_b['force_field_type'],
                                                  atom_c['force_field_type'],
                                                  atom_d['force_field_type'])
                    except AttributeError:
                        pass
            string += "\nAngleAngleTorsion Coeffs\n\n"
            for key in sorted(self.unique_dihedral_types.keys()):
                a, b, c, d, dihedral = self.unique_dihedral_types[key]
                atom_a, atom_b, atom_c, atom_d = self.graph.node[a], \
                                                 self.graph.node[b], \
                                                 self.graph.node[c], \
                                                 self.graph.node[d]
                if (dihedral['potential'].name!="class2"):
                    string += "%5i skip "%(key)
                    string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                              atom_b['force_field_type'],
                                              atom_c['force_field_type'],
                                              atom_d['force_field_type'])
                else:
                    try:
                        string += "%5i %s "%(key, dihedral['potential'].aat)
                        string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                                  atom_b['force_field_type'],
                                                  atom_c['force_field_type'],
                                                  atom_d['force_field_type'])
                    except AttributeError:
                        pass
            string += "\nBondBond13 Coeffs\n\n"
            for key in sorted(self.unique_dihedral_types.keys()):
                a, b, c, d, dihedral = self.unique_dihedral_types[key]
                atom_a, atom_b, atom_c, atom_d = self.graph.node[a], \
                                                 self.graph.node[b], \
                                                 self.graph.node[c], \
                                                 self.graph.node[d]
                if (dihedral['potential'].name!="class2"):
                    string += "%5i skip "%(key)
                    string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                              atom_b['force_field_type'],
                                              atom_c['force_field_type'],
                                              atom_d['force_field_type'])
                else:
                    try:
                        string += "%5i %s "%(key, dihedral['potential'].bb13)
                        string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                                     atom_b['force_field_type'],
                                                     atom_c['force_field_type'],
                                                     atom_d['force_field_type'])
                    except AttributeError:
                        pass


        class2improper = False
        if (len(self.unique_improper_types.keys()) > 0):
            string += "\nImproper Coeffs\n\n"
            for key in sorted(self.unique_improper_types.keys()):
                a, b, c, d, improper = self.unique_improper_types[key]
                atom_a, atom_b, atom_c, atom_d = self.graph.node[a], \
                                                 self.graph.node[b], \
                                                 self.graph.node[c], \
                                                 self.graph.node[d]

                if improper['potential'] is None:
                    no_improper.append("%5i : %s %s %s %s"%(key,
                        atom_a['force_field_type'],
                        atom_b['force_field_type'],
                        atom_c['force_field_type'],
                        atom_d['force_field_type']))
                else:
                    if(improper['potential'].name == "class2"):
                        class2improper = True
                    string += "%5i %s "%(key, improper['potential'])
                    string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                                 atom_b['force_field_type'],
                                                 atom_c['force_field_type'],
                                                 atom_d['force_field_type'])
        if (class2improper):
            string += "\nAngleAngle Coeffs\n\n"
            for key in sorted(self.unique_improper_types.keys()):
                a, b, c, d, improper = self.unique_improper_types[key]
                atom_a, atom_b, atom_c, atom_d = self.graph.node[a], \
                                                 self.graph.node[b], \
                                                 self.graph.node[c], \
                                                 self.graph.node[d]
                if (improper['potential'].name!="class2"):
                    string += "%5i skip "%(key)
                    string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                                 atom_b['force_field_type'],
                                                 atom_c['force_field_type'],
                                                 atom_d['force_field_type'])
                else:
                    try:
                        string += "%5i %s "%(key, improper['potential'].aa)
                        string += "# %s %s %s %s\n"%(atom_a['force_field_type'],
                                                     atom_b['force_field_type'],
                                                     atom_c['force_field_type'],
                                                     atom_d['force_field_type'])
                    except AttributeError:
                        pass

        if((len(self.unique_pair_types.keys()) > 0) and (self.pair_in_data)):
            string += "\nPair Coeffs\n\n"
            for key, (n,pair) in sorted(self.unique_atom_types.items()):
                #pair = self.graph.node[n]
                string += "%5i %s "%(key, pair['pair_potential'])
                string += "# %s %s\n"%(pair['force_field_type'],
                                       pair['force_field_type'])


        # Nest this in an if statement
        if any([no_bond, no_angle, no_dihedral, no_improper]):
        # WARNING MESSAGE for potentials we think are unique but have not been calculated
            print("WARNING: The following unique bonds/angles/dihedrals/impropers" +
                    " were detected in your crystal")
            print("But they have not been assigned a potential from user_input.txt"+
                    " or from an internal FF assignment routine!")
            print("Bonds")
            for elem in no_bond:
                print(elem)
            print("Angles")
            for elem in no_angle:
                print(elem)
            print("Dihedrals")
            for elem in no_dihedral:
                print(elem)
            print("Impropers")
            for elem in no_improper:
                print(elem)
            print("If you think you specified one of these in your user_input.txt " +
                  "and this is an error, please contact developers\n")
            print("CONTINUING...")


        #************[atoms]************
    	# Added 1 to all atom, bond, angle, dihedral, improper indices (LAMMPS does not accept atom of index 0)
        sorted_nodes = sorted(self.graph.nodes())
        if(len(self.unique_atom_types.keys()) > 0):
            string += "\nAtoms\n\n"
            for node in sorted_nodes:
                atom = self.graph.node[node]
                string += "%8i %8i %8i %11.5f %10.5f %10.5f %10.5f\n"%(node,
                                                                       atom['molid'],
                                                                       atom['ff_type_index'],
                                                                       atom['charge'],
                                                                       atom['cartesian_coordinates'][0],
                                                                       atom['cartesian_coordinates'][1],
                                                                       atom['cartesian_coordinates'][2])

        #************[bonds]************
        if(len(self.unique_bond_types.keys()) > 0):
            string += "\nBonds\n\n"
            idx = 0
            for n1, n2, bond in sorted(list(self.graph.edges_iter2(data=True))):
                idx += 1
                string += "%8i %8i %8i %8i\n"%(idx,
                                               bond['ff_type_index'],
                                               n1,
                                               n2)

        #************[angles]***********
        if(len(self.unique_angle_types.keys()) > 0):
            string += "\nAngles\n\n"
            idx = 0
            for node in sorted_nodes:
                atom = self.graph.node[node]
                try:
                    for (a, c), angle in list(atom['angles'].items()):
                        idx += 1
                        string += "%8i %8i %8i %8i %8i\n"%(idx,
                                                           angle['ff_type_index'],
                                                           a,
                                                           node,
                                                           c)
                except KeyError:
                    pass

        #************[dihedrals]********
        if(len(self.unique_dihedral_types.keys()) > 0):
            string += "\nDihedrals\n\n"
            idx = 0
            for n1, n2, data in sorted(list(self.graph.edges_iter2(data=True))):
                try:
                    for (a, d), dihedral in list(data['dihedrals'].items()):
                        idx+=1
                        string += "%8i %8i %8i %8i %8i %8i\n"%(idx,
                                                              dihedral['ff_type_index'],
                                                              a,
                                                              n1,
                                                              n2,
                                                              d)
                except KeyError:
                    pass
        #************[impropers]********
        if(len(self.unique_improper_types.keys()) > 0):
            string += "\nImpropers\n\n"
            idx = 0
            for node in sorted_nodes:
                atom = self.graph.node[node]
                try:
                    for (a, c, d), improper in list(atom['impropers'].items()):
                        idx += 1
                        string += "%8i %8i %8i %8i %8i %8i\n"%(idx,
                                                               improper['ff_type_index'],
                                                               node,
                                                               a,
                                                               c,
                                                               d)
                except KeyError:
                    pass

        return string
    def fixcount(self, count=[]):
        count.append(1)
        return (len(count))

    def construct_input_file(self):
        """Input file construction based on user-defined inputs.

        NB: This function is getting huge. We should probably break it
        up into logical sub-sections.

        """
        inp_str = ""

        inp_str += "%-15s %s\n"%("log","log.%s append"%(self.name))
        inp_str += "%-15s %s\n"%("units","real")
        inp_str += "%-15s %s\n"%("atom_style","full")
        inp_str += "%-15s %s\n"%("boundary","p p p")
        inp_str += "\n"
        if(len(self.unique_pair_types.keys()) > 0):
            inp_str += "%-15s %s\n"%("pair_style", self.pair_style)
        if(len(self.unique_bond_types.keys()) > 0):
            inp_str += "%-15s %s\n"%("bond_style", self.bond_style)
        if(len(self.unique_angle_types.keys()) > 0):
            inp_str += "%-15s %s\n"%("angle_style", self.angle_style)
        if(len(self.unique_dihedral_types.keys()) > 0):
            inp_str += "%-15s %s\n"%("dihedral_style", self.dihedral_style)
        if(len(self.unique_improper_types.keys()) > 0):
            inp_str += "%-15s %s\n"%("improper_style", self.improper_style)
        if(self.kspace_style):
            inp_str += "%-15s %s\n"%("kspace_style", self.kspace_style)
        inp_str += "\n"

        # general catch-all for extra force field commands needed.
        inp_str += "\n".join(list(set(self.special_commands)))
        inp_str += "\n"
        inp_str += "%-15s %s\n"%("box tilt","large")
        inp_str += "%-15s %s\n"%("read_data","data.%s"%(self.name))

        if(not self.pair_in_data):
            inp_str += "#### Pair Coefficients ####\n"
            for pair,data in sorted(self.unique_pair_types.items()):
                n1, n2 = self.unique_atom_types[pair[0]][0], self.unique_atom_types[pair[1]][0]

                try:
                    if pair[2] == 'hb':
                        inp_str += "%-15s %-4i %-4i %s # %s %s\n"%("pair_coeff",
                            pair[0], pair[1], data['h_bond_potential'],
                            self.graph.node[n1]['force_field_type'],
                            self.graph.node[n2]['force_field_type'])
                    elif pair[2] == 'table':
                        inp_str += "%-15s %-4i %-4i %s # %s %s\n"%("pair_coeff",
                            pair[0], pair[1], data['table_potential'],
                            self.graph.node[n1]['force_field_type'],
                            self.graph.node[n2]['force_field_type'])
                    else:
                        inp_str += "%-15s %-4i %-4i %s # %s %s\n"%("pair_coeff",
                            pair[0], pair[1], data['pair_potential'],
                            self.graph.node[n1]['force_field_type'],
                            self.graph.node[n2]['force_field_type'])
                except IndexError:
                    pass
            inp_str += "#### END Pair Coefficients ####\n\n"

        inp_str += "\n#### Atom Groupings ####\n"
        # Define a group for the template molecules, if they exist.
        # It is conceptually hard to rationalize why this has to be
        # a separate command and not combined with the 'molecule' command
        if self.options.insert_molecule:
            moltypes = []
            for mnode, mdata in self.template_molecule.nodes_iter2(data=True):
                moltypes.append(mdata['ff_type_index'])

            inp_str += "%-15s %s type   "%("group", self.options.insert_molecule)
            for x in self.groups(list(set(moltypes))):
                x = list(x)
                if (len(x) > 1):
                    inp_str += " %i:%i"%(x[0], x[-1])
                else:
                    inp_str += " %i"%(x[0])
            inp_str += "\n"


        framework_atoms = list(self.graph.nodes())
        if(self.molecules)and(len(self.molecule_types.keys()) < 32):
            # lammps cannot handle more than 32 groups including 'all'
            total_count = 0
            for k,v in self.molecule_types.items():
                total_count += len(v)
            list_individual_molecules = True
            if total_count > 31:
                list_individual_molecules = False

            idx = 1
            for mtype in list(self.molecule_types.keys()):

                inp_str += "%-15s %-8s %s  "%("group", "%i"%(mtype), "id")
                all_atoms = []
                for j in self.molecule_types[mtype]:
                    all_atoms += nx.read_gpickle(self.subgraphs[j]).nodes()
                for x in self.groups(all_atoms):
                    x = list(x)
                    if(len(x)>1):
                        inp_str += " %i:%i"%(x[0], x[-1])
                    else:
                        inp_str += " %i"%(x[0])
                inp_str += "\n"
                for atom in reversed(sorted(all_atoms)):
                    del framework_atoms[framework_atoms.index(atom)]
                mcount = 0
                if list_individual_molecules:
                    for j in self.molecule_types[mtype]:
                        sg = nx.read_gpickle(self.subgraphs[j])
                        if (sg.molecule_images):
                            for molecule in sg.molecule_images:
                                mcount += 1
                                inp_str += "%-15s %-8s %s  "%("group", "%i-%i"%(mtype, mcount), "id")
                                for x in self.groups(molecule):
                                    x = list(x)
                                    if(len(x)>1):
                                        inp_str += " %i:%i"%(x[0], x[-1])
                                    else:
                                        inp_str += " %i"%(x[0])
                                inp_str += "\n"
                        elif len(self.molecule_types[mtype]) > 1:
                            mcount += 1
                            inp_str += "%-15s %-8s %s  "%("group", "%i-%i"%(mtype, mcount), "id")
                            molecule = sg.nodes()
                            for x in self.groups(molecule):
                                x = list(x)
                                if(len(x)>1):
                                    inp_str += " %i:%i"%(x[0], x[-1])
                                else:
                                    inp_str += " %i"%(x[0])
                            inp_str += "\n"

            if(not framework_atoms):
                self.framework = False
        if(self.framework):
            inp_str += "%-15s %-8s %s  "%("group", "fram", "id")
            for x in self.groups(framework_atoms):
                x = list(x)
                if(len(x)>1):
                    inp_str += " %i:%i"%(x[0], x[-1])
                else:
                    inp_str += " %i"%(x[0])
            inp_str += "\n"
        inp_str += "#### END Atom Groupings ####\n\n"

        if self.options.dump_dcd:
            inp_str += "%-15s %s\n"%("dump","%s_dcdmov all dcd %i %s_mov.dcd"%
                            (self.name, self.options.dump_dcd, self.name))
        elif self.options.dump_xyz:
            inp_str += "%-15s %s\n"%("dump","%s_xyzmov all xyz %i %s_mov.xyz"%
                                (self.name, self.options.dump_xyz, self.name))
            inp_str += "%-15s %s\n"%("dump_modify", "%s_xyzmov element %s"%(
                                     self.name,
                                     " ".join([self.unique_atom_types[key][1]['element']
                                                for key in sorted(self.unique_atom_types.keys())])))
        elif self.options.dump_lammpstrj:
            inp_str += "%-15s %s\n"%("dump","%s_lammpstrj all atom %i %s_mov.lammpstrj"%
                                (self.name, self.options.dump_lammpstrj, self.name))

            # in the meantime we need to map atom id to element that will allow us to
            # post-process the lammpstrj file and create a cif out of each
            # snapshot stored in the trajectory
            f = open("lammpstrj_to_element.txt", "w")
            for key in sorted(self.unique_atom_types.keys()):
                f.write("%s\n"%(self.unique_atom_types[key][1]['element']))
            f.close()

        if (self.options.minimize):
            box_min = "aniso"
            min_style = "cg"
            min_eval = 1e-6   # HKUST-1 will not minimize past 1e-11
            max_iterations = 100000 # if the minimizer can't reach a minimum in this many steps,
                                    # change the min_eval to something higher.
            #inp_str += "%-15s %s\n"%("min_style","fire")
            #inp_str += "%-15s %i %s\n"%("compute", 1, "all msd com yes")
            #inp_str += "%-15s %-10s %s\n"%("variable", "Dx", "equal c_1[1]")
            #inp_str += "%-15s %-10s %s\n"%("variable", "Dy", "equal c_1[2]")
            #inp_str += "%-15s %-10s %s\n"%("variable", "Dz", "equal c_1[3]")
            #inp_str += "%-15s %-10s %s\n"%("variable", "MSD", "equal c_1[4]")
            #inp_str += "%-15s %s %s\n"%("fix", "output all print 1", "\"$(vol),$(cella),$(cellb),$(cellc),${Dx},${Dy},${Dz},${MSD}\"" +
            #                                " file %s.min.csv title \"Vol,CellA,CellB,CellC,Dx,Dy,Dz,MSD\" screen no"%(self.name))
            inp_str += "%-15s %s\n"%("min_style", min_style)
            inp_str += "%-15s %s\n"%("print", "\"MinStep,CellMinStep,AtomMinStep,FinalStep,Energy,EDiff\"" +
                                              " file %s.min.csv screen no"%(self.name))
            inp_str += "%-15s %-10s %s\n"%("variable", "min_eval", "equal %.2e"%(min_eval))
            inp_str += "%-15s %-10s %s\n"%("variable", "prev_E", "equal %.2f"%(50000.)) # set unreasonably high for first loop
            inp_str += "%-15s %-10s %s\n"%("variable", "iter", "loop %i"%(max_iterations))
            inp_str += "%-15s %s\n"%("label", "loop_min")

            fix = self.fixcount()
            inp_str += "%-15s %s\n"%("min_style", min_style)
            inp_str += "%-15s %s\n"%("fix","%i all box/relax %s 0.0 vmax 0.01"%(fix, box_min))
            inp_str += "%-15s %s\n"%("minimize","1.0e-15 1.0e-15 10000 100000")
            inp_str += "%-15s %s\n"%("unfix", "%i"%fix)
            inp_str += "%-15s %s\n"%("min_style", "fire")
            inp_str += "%-15s %-10s %s\n"%("variable", "tempstp", "equal $(step)")
            inp_str += "%-15s %-10s %s\n"%("variable", "CellMinStep", "equal ${tempstp}")
            inp_str += "%-15s %s\n"%("minimize","1.0e-15 1.0e-15 10000 100000")
            inp_str += "%-15s %-10s %s\n"%("variable", "AtomMinStep", "equal ${tempstp}")
            inp_str += "%-15s %-10s %s\n"%("variable", "temppe", "equal $(pe)")
            inp_str += "%-15s %-10s %s\n"%("variable", "min_E", "equal abs(${prev_E}-${temppe})")
            inp_str += "%-15s %s\n"%("print", "\"${iter},${CellMinStep},${AtomMinStep},${AtomMinStep}," +
                                              "$(pe),${min_E}\"" +
                                              " append %s.min.csv screen no"%(self.name))

            inp_str += "%-15s %s\n"%("if","\"${min_E} < ${min_eval}\" then \"jump SELF break_min\"")
            inp_str += "%-15s %-10s %s\n"%("variable", "prev_E", "equal ${temppe}")
            inp_str += "%-15s %s\n"%("next", "iter")
            inp_str += "%-15s %s\n"%("jump", "SELF loop_min")
            inp_str += "%-15s %s\n"%("label", "break_min")

           # inp_str += "%-15s %s\n"%("unfix", "output")
        # delete bond types etc, for molecules that are rigid

        if self.options.insert_molecule:
            inp_str += "%-15s %s %s.molecule\n"%("molecule", self.options.insert_molecule, self.options.insert_molecule)

        for mol in sorted(self.molecule_types.keys()):
            rep = nx.read_gpickle(self.subgraphs[self.molecule_types[mol][0]])
            if rep.rigid:
                inp_str += "%-15s %s\n"%("neigh_modify", "exclude molecule %i"%(mol))
                # find and delete all bonds, angles, dihedrals, and impropers associated
                # with this molecule, as they will consume unnecessary amounts of CPU time
                inp_str += "%-15s %i %s\n"%("delete_bonds", mol, "multi remove")

        if (self.fix_shake):
            shake_tol = 0.0001
            iterations = 20
            print_every = 0  # maybe set to non-zero, but output files could become huge.
            shk_fix = self.fixcount()
            shake_str = "b "+" ".join(["%i"%i for i in self.fix_shake['bonds']]) + \
                        " a " + " ".join(["%i"%i for i in self.fix_shake['angles']])
                       # fix  id group tolerance iterations print_every [bonds + angles]
            inp_str += "%-15s %i %s %s %f %i %i %s\n"%('fix', shk_fix, 'all', 'shake', shake_tol, iterations, print_every, shake_str)

        if (self.options.random_vel):
            inp_str += "%-15s %s\n"%("velocity", "all create %.2f %i"%(self.options.temp, np.random.randint(1,3000000)))

        if (self.options.nvt):
            inp_str += "%-15s %-10s %s\n"%("variable", "dt", "equal %.2f"%(1.0))
            inp_str += "%-15s %-10s %s\n"%("variable", "tdamp", "equal 100*${dt}")
            molecule_fixes = []
            mollist = sorted(list(self.molecule_types.keys()))

            if self.options.insert_molecule:
                id = self.fixcount()
                molecule_fixes.append(id)
                if self.template_molecule.rigid:
                    insert_rigid_id = id
                    inp_str += "%-15s %s\n"%("fix", "%i %s rigid/small molecule langevin %.2f %.2f ${tdamp} %i mol %s"%(id,
                                                                                            self.options.insert_molecule,
                                                                                            self.options.temp,
                                                                                            self.options.temp,
                                                                                            np.random.randint(1,3000000),
                                                                                            self.options.insert_molecule
                                                                                            ))
                else:
                    # no idea if this will work..
                    inp_str += "%-15s %s\n"%("fix", "%i %s langevin %.2f %.2f ${tdamp} %i"%(id,
                                                                                        self.options.insert_molecule,
                                                                                        self.options.temp,
                                                                                        self.options.temp,
                                                                                        np.random.randint(1,3000000)
                                                                                        ))
                    id = self.fixcount()
                    molecule_fixes.append(id)
                    inp_str += "%-15s %s\n"%("fix", "%i %i nve"%(id,molid))


            for molid in mollist:
                id = self.fixcount()
                molecule_fixes.append(id)
                rep = nx.read_gpickle(self.subgraphs[self.molecule_types[molid][0]])
                if(rep.rigid):
                    inp_str += "%-15s %s\n"%("fix", "%i %s rigid/small molecule langevin %.2f %.2f ${tdamp} %i"%(id,
                                                                                            str(molid),
                                                                                            self.options.temp,
                                                                                            self.options.temp,
                                                                                            np.random.randint(1,3000000)
                                                                                            ))
                else:
                    inp_str += "%-15s %s\n"%("fix", "%i %s langevin %.2f %.2f ${tdamp} %i"%(id,
                                                                                        str(molid),
                                                                                        self.options.temp,
                                                                                        self.options.temp,
                                                                                        np.random.randint(1,3000000)
                                                                                        ))
                    id = self.fixcount()
                    molecule_fixes.append(id)
                    inp_str += "%-15s %s\n"%("fix", "%i %i nve"%(id,molid))
            if self.framework:
                id = self.fixcount()
                molecule_fixes.append(id)
                inp_str += "%-15s %s\n"%("fix", "%i %s langevin %.2f %.2f ${tdamp} %i"%(id,
                                                                                        "fram",
                                                                                        self.options.temp,
                                                                                        self.options.temp,
                                                                                        np.random.randint(1,3000000)
                                                                                        ))
                id = self.fixcount()
                molecule_fixes.append(id)
                inp_str += "%-15s %s\n"%("fix", "%i fram nve"%id)

            # deposit within nvt equilibrium phase.  TODO(pboyd): This entire input file formation Needs to be re-thought.
            if self.options.deposit:
                deposit = self.options.deposit * np.prod(np.array(self.supercell))

                # add a shift of the cell as the deposit of molecules tends to shift things.
                id = self.fixcount()
                inp_str += "%-15s %i all momentum 1 linear 1 1 1 angular\n"%("fix", id)
                id = self.fixcount()
                # define a region the size of the unit cell.
                every = self.options.neqstp/2/deposit
                if every <= 100:
                    print("WARNING: you have set %i equilibrium steps, which may not be enough to "%(self.options.neqstp) +
                            "deposit %i %s molecules. "%(deposit, self.options.insert_molecule) +
                            "The metric used to create this warning is NEQSTP/2/DEPOSIT. So adjust accordingly.")
                inp_str += "%-15s %-8s %-8s %i %s %i %s %i %s %s\n"%("region", "cell", "block", 0, "EDGE",
                                                                     0, "EDGE", 0, "EDGE", "units lattice")
                inp_str += "%-15s %i %s %s %i %i %i %i %s %s %s %.2f %s %s"%("fix", id, self.options.insert_molecule,
                                                                             "deposit", deposit, 0, every,
                                                                             np.random.randint(1, 3000000), "region",
                                                                             "cell", "near", 2.0, "mol",
                                                                             self.options.insert_molecule)
                molecule_fixes.append(id)
                # need rigid fixid
                if self.template_molecule.rigid:
                    inp_str += " rigid %i\n"%(insert_rigid_id)
                else:
                    inp_str += "\n"

            inp_str += "%-15s %i\n"%("thermo", 0)
            inp_str += "%-15s %i\n"%("run", self.options.neqstp)
            while(molecule_fixes):
                fid = molecule_fixes.pop(0)
                inp_str += "%-15s %i\n"%("unfix", fid)

            if self.options.insert_molecule:
                id = self.fixcount()
                molecule_fixes.append(id)
                if self.template_molecule.rigid:
                    inp_str += "%-15s %s\n"%("fix", "%i %s rigid/nvt/small molecule temp %.2f %.2f ${tdamp} mol %s"%(id,
                                                                                            self.options.insert_molecule,
                                                                                            self.options.temp,
                                                                                            self.options.temp,
                                                                                            self.options.insert_molecule
                                                                                            ))
                else:
                    # no idea if this will work..
                    inp_str += "%-15s %s\n"%("fix", "%i %s nvt %.2f %.2f ${tdamp}"%(id,
                                                                                        self.options.insert_molecule,
                                                                                        self.options.temp,
                                                                                        self.options.temp
                                                                                        ))


            for molid in mollist:
                id = self.fixcount()
                molecule_fixes.append(id)
                rep = nx.read_gpickle(self.subgraphs[self.molecule_types[molid][0]])
                if(rep.rigid):
                    inp_str += "%-15s %s\n"%("fix", "%i %s rigid/nvt/small molecule temp %.2f %.2f ${tdamp}"%(id,
                                                                                            str(molid),
                                                                                            self.options.temp,
                                                                                            self.options.temp
                                                                                            ))
                else:
                    inp_str += "%-15s %s\n"%("fix", "%i %s nvt temp %.2f %.2f ${tdamp}"%(id,
                                                                                   str(molid),
                                                                                   self.options.temp,
                                                                                   self.options.temp
                                                                                   ))
            if self.framework:
                id = self.fixcount()
                molecule_fixes.append(id)
                inp_str += "%-15s %s\n"%("fix", "%i %s nvt temp %.2f %.2f ${tdamp}"%(id,
                                                                                   "fram",
                                                                                   self.options.temp,
                                                                                   self.options.temp
                                                                                   ))

            inp_str += "%-15s %i\n"%("thermo", 1)
            inp_str += "%-15s %i\n"%("run", self.options.nprodstp)

            while(molecule_fixes):
                fid = molecule_fixes.pop(0)
                inp_str += "%-15s %i\n"%("unfix", fid)

        #TODO(pboyd): add molecule commands to npt simulations.. this needs to be separated!
        if (self.options.npt):
            id = self.fixcount()
            inp_str += "%-15s %-10s %s\n"%("variable", "dt", "equal %.2f"%(1.0))
            inp_str += "%-15s %-10s %s\n"%("variable", "pdamp", "equal 1000*${dt}")
            inp_str += "%-15s %-10s %s\n"%("variable", "tdamp", "equal 100*${dt}")

            inp_str += "%-15s %s\n"%("fix", "%i all npt temp %.2f %.2f ${tdamp} tri %.2f %.2f ${pdamp}"%(id, self.options.temp, self.options.temp,
                                                                                                        self.options.pressure, self.options.pressure))
            inp_str += "%-15s %i\n"%("thermo", 0)
            inp_str += "%-15s %i\n"%("run", self.options.neqstp)
            inp_str += "%-15s %i\n"%("thermo", 1)
            inp_str += "%-15s %i\n"%("run", self.options.nprodstp)

            inp_str += "%-15s %i\n"%("unfix", id)

        if(self.options.bulk_moduli):
            min_style=True
            thermo_style=False

            inp_str += "\n%-15s %s\n"%("dump", "str all atom 1 initial_structure.dump")
            inp_str += "%-15s\n"%("run 0")
            inp_str += "%-15s %-10s %s\n"%("variable", "rs", "equal step")
            inp_str += "%-15s %-10s %s\n"%("variable", "readstep", "equal ${rs}")
            inp_str += "%-15s %-10s %s\n"%("variable", "rs", "delete")
            inp_str += "%-15s %s\n"%("undump", "str")

            if thermo_style:
                inp_str += "\n%-15s %-10s %s\n"%("variable", "simTemp", "equal %.4f"%(self.options.temp))
                inp_str += "%-15s %-10s %s\n"%("variable", "dt", "equal %.2f"%(1.0))
                inp_str += "%-15s %-10s %s\n"%("variable", "tdamp", "equal 100*${dt}")
            elif min_style:
                inp_str += "%-15s %s\n"%("min_style","fire")
            inp_str += "%-15s %-10s %s\n"%("variable", "at", "equal cella")
            inp_str += "%-15s %-10s %s\n"%("variable", "bt", "equal cellb")
            inp_str += "%-15s %-10s %s\n"%("variable", "ct", "equal cellc")
            inp_str += "%-15s %-10s %s\n"%("variable", "a", "equal ${at}")
            inp_str += "%-15s %-10s %s\n"%("variable", "b", "equal ${bt}")
            inp_str += "%-15s %-10s %s\n"%("variable", "c", "equal ${ct}")
            inp_str += "%-15s %-10s %s\n"%("variable", "at", "delete")
            inp_str += "%-15s %-10s %s\n"%("variable", "bt", "delete")
            inp_str += "%-15s %-10s %s\n"%("variable", "ct", "delete")

            inp_str += "%-15s %-10s %s\n"%("variable", "N", "equal %i"%self.options.iter_count)
            inp_str += "%-15s %-10s %s\n"%("variable", "totDev", "equal %.5f"%self.options.max_dev)
            inp_str += "%-15s %-10s %s\n"%("variable", "sf", "equal ${totDev}/${N}*2")
            inp_str += "%-15s %s\n"%("print", "\"Loop,CellScale,Vol,Pressure,E_total,E_pot,E_kin" +
                                              ",E_bond,E_angle,E_torsion,E_imp,E_vdw,E_coul\""+
                                              " file %s.output.csv screen no"%(self.name))
            inp_str += "%-15s %-10s %s\n"%("variable", "do", "loop ${N}")
            inp_str += "%-15s %s\n"%("label", "loop_bulk")
            inp_str += "%-15s %s\n"%("read_dump", "initial_structure.dump ${readstep} x y z box yes format native")
            inp_str += "%-15s %-10s %s\n"%("variable", "scaleVar", "equal 1.00-${totDev}+${do}*${sf}")
            inp_str += "%-15s %-10s %s\n"%("variable", "scaleA", "equal ${scaleVar}*${a}")
            inp_str += "%-15s %-10s %s\n"%("variable", "scaleB", "equal ${scaleVar}*${b}")
            inp_str += "%-15s %-10s %s\n"%("variable", "scaleC", "equal ${scaleVar}*${c}")
            inp_str += "%-15s %s\n"%("change_box", "all x final 0.0 ${scaleA} y final 0.0 ${scaleB} z final 0.0 ${scaleC} remap")
            if (min_style):
                inp_str += "%-15s %s\n"%("minimize", "1.0e-15 1.0e-15 10000 100000")
                inp_str += "%-15s %s\n"%("print", "\"${do},${scaleVar},$(vol),$(press),$(etotal),$(pe),$(ke)"+
                                              ",$(ebond),$(eangle),$(edihed),$(eimp),$(evdwl),$(ecoul)\""+
                                              " append %s.output.csv screen no"%(self.name))
            elif (thermo_style):
                inp_str += "%-15s %s\n"%("velocity", "all create ${simTemp} %i"%(np.random.randint(1,3000000)))
                inp_str += "%-15s %s %s %s \n"%("fix", "bm", "all nvt", "temp ${simTemp} ${simTemp} ${tdamp} tchain 5")
                inp_str += "%-15s %i\n"%("run", self.options.neqstp)
                #inp_str += "%-15s %s\n"%("print", "\"STEP ${do} ${scaleVar} $(vol) $(press) $(etotal)\"")
                inp_str += "%-15s %s %s\n"%("fix", "output all print 10", "\"${do},${scaleVar},$(vol),$(press),$(etotal),$(pe),$(ke)" +
                                            ",$(ebond),$(eangle),$(edihed),$(eimp),$(evdwl),$(ecoul)\""+
                                            " append %s.output.csv screen no"%(self.name))
                inp_str += "%-15s %i\n"%("run", self.options.nprodstp)
                inp_str += "%-15s %s\n"%("unfix", "output")
                inp_str += "%-15s %s\n"%("unfix", "bm")
            inp_str += "%-15s %-10s %s\n"%("variable", "scaleVar", "delete")
            inp_str += "%-15s %-10s %s\n"%("variable", "scaleA", "delete")
            inp_str += "%-15s %-10s %s\n"%("variable", "scaleB", "delete")
            inp_str += "%-15s %-10s %s\n"%("variable", "scaleC", "delete")
            inp_str += "%-15s %s\n"%("next", "do")
            inp_str += "%-15s %s\n"%("jump", "SELF loop_bulk")
            inp_str += "%-15s %-10s %s\n"%("variable", "do", "delete")

        if (self.options.thermal_scaling):
            temperature = self.options.temp # kelvin
            equil_steps = self.options.neqstp
            prod_steps = self.options.nprodstp
            temprange = np.linspace(temperature, self.options.max_dev, self.options.iter_count).tolist()
            temprange.append(298.0)
            temprange.insert(0,1.0) # add 1 and 298 K simulations.

            inp_str += "\n%-15s %s\n"%("dump", "str all atom 1 initial_structure.dump")
            inp_str += "%-15s\n"%("run 0")
            inp_str += "%-15s %-10s %s\n"%("variable", "rs", "equal step")
            inp_str += "%-15s %-10s %s\n"%("variable", "readstep", "equal ${rs}")
            inp_str += "%-15s %-10s %s\n"%("variable", "rs", "delete")
            inp_str += "%-15s %s\n"%("undump", "str")

            inp_str += "%-15s %-10s %s\n"%("variable", "sim_temp", "index %s"%(" ".join(["%.2f"%i for i in sorted(temprange)])))
            inp_str += "%-15s %-10s %s\n"%("variable", "sim_press", "equal %.3f"%self.options.pressure) # atmospheres.
            #inp_str += "%-15s %-10s %s\n"%("variable", "a", "equal cella")
            #inp_str += "%-15s %-10s %s\n"%("variable", "myVol", "equal vol")
            #inp_str += "%-15s %-10s %s\n"%("variable", "t", "equal temp")
            # timestep in femtoseconds
            inp_str += "%-15s %-10s %s\n"%("variable", "dt", "equal %.2f"%(1.0))
            inp_str += "%-15s %-10s %s\n"%("variable", "pdamp", "equal 1000*${dt}")
            inp_str += "%-15s %-10s %s\n"%("variable", "tdamp", "equal 100*${dt}")
            inp_str += "%-15s %s\n"%("print", "\"Step,Temp,CellA,Vol\" file %s.output.csv screen no"%(self.name))
            inp_str += "%-15s %s\n"%("label", "loop_thermal")
            #fix1 = self.fixcount()

            inp_str += "%-15s %s\n"%("read_dump", "initial_structure.dump ${readstep} x y z box yes format native")
            inp_str += "%-15s %s\n"%("thermo_style", "custom step temp cella cellb cellc vol etotal")

            # the ave/time fix must be after read_dump, or the averages are reported as '0'
            #inp_str += "%-15s %s\n"%("fix", "%i all ave/time 1 %i %i v_t v_a v_myVol ave one"%(fix1, prod_steps,
            #                                                                                   prod_steps + equil_steps))
            molecule_fixes = []
            mollist = sorted(list(self.molecule_types.keys()))
            for molid in mollist:
                id = self.fixcount()
                molecule_fixes.append(id)
                rep = nx.read_gpickle(self.subgraphs[self.molecule_types[molid][0]])
                if(rep.rigid):
                    inp_str += "%-15s %s\n"%("fix", "%i %s rigid/small molecule langevin ${sim_temp} ${sim_temp} ${tdamp} %i"%(id,
                                                                                            str(molid),
                                                                                            np.random.randint(1,3000000)
                                                                                            ))
                else:
                    inp_str += "%-15s %s\n"%("fix", "%i %s langevin ${sim_temp} ${sim_temp} ${tdamp} %i"%(id,
                                                                                        str(molid),
                                                                                        np.random.randint(1,3000000)
                                                                                        ))
                    id = self.fixcount()
                    molecule_fixes.append(id)
                    inp_str += "%-15s %s\n"%("fix", "%i %i nve"%(id,molid))
            if self.framework:
                id = self.fixcount()
                molecule_fixes.append(id)
                inp_str += "%-15s %s\n"%("fix", "%i %s langevin ${sim_temp} ${sim_temp} ${tdamp} %i"%(id,
                                                                                        "fram",
                                                                                        np.random.randint(1,3000000)
                                                                                        ))
                id = self.fixcount()
                molecule_fixes.append(id)
                inp_str += "%-15s %s\n"%("fix", "%i fram nve"%id)
            inp_str += "%-15s %i\n"%("thermo", 0)
            inp_str += "%-15s %i\n"%("run", equil_steps)
            while(molecule_fixes):
                fid = molecule_fixes.pop(0)
                inp_str += "%-15s %i\n"%("unfix", fid)
            id = self.fixcount()
            # creating velocity may cause instability at high temperatures.
            #inp_str += "%-15s %s\n"%("velocity", "all create 50 %i"%(np.random.randint(1,3000000)))
            inp_str += "%-15s %i %s %s %s %s\n"%("fix", id,
                                        "all npt",
                                        "temp ${sim_temp} ${sim_temp} ${tdamp}",
                                        "tri ${sim_press} ${sim_press} ${pdamp}",
                                        "tchain 5 pchain 5")
            inp_str += "%-15s %i\n"%("thermo", 0)
            inp_str += "%-15s %i\n"%("run", equil_steps)
            inp_str += "%-15s %s %s\n"%("fix", "output all print 10", "\"${sim_temp},$(temp),$(cella),$(vol)\"" +
                                        " append %s.output.csv screen no"%(self.name))
            #inp_str += "%-15s %i\n"%("thermo", 10)
            inp_str += "%-15s %i\n"%("run", prod_steps)
            inp_str += "%-15s %s\n"%("unfix", "output")
            #inp_str += "\n%-15s %-10s %s\n"%("variable", "inst_t", "equal f_%i[1]"%(fix1))
            #inp_str += "%-15s %-10s %s\n"%("variable", "inst_a", "equal f_%i[2]"%(fix1))
            #inp_str += "%-15s %-10s %s\n"%("variable", "inst_v", "equal f_%i[3]"%(fix1))

            #inp_str += "%-15s %-10s %s\n"%("variable", "inst_t", "delete")
            #inp_str += "%-15s %-10s %s\n"%("variable", "inst_a", "delete")
            #inp_str += "%-15s %-10s %s\n\n"%("variable", "inst_v", "delete")
            inp_str += "%-15s %i\n"%("unfix", id)
            #inp_str += "%-15s %i\n"%("unfix", fix1)
            inp_str += "\n%-15s %s\n"%("next", "sim_temp")
            inp_str += "%-15s %s\n"%("jump", "SELF loop_thermal")
            inp_str += "%-15s %-10s %s\n"%("variable", "sim_temp", "delete")

        if self.options.dump_dcd:
            inp_str += "%-15s %s\n"%("undump", "%s_dcdmov"%(self.name))
        elif self.options.dump_xyz:
            inp_str += "%-15s %s\n"%("undump", "%s_xyzmov"%(self.name))
        elif self.options.dump_lammpstrj:
            inp_str += "%-15s %s\n"%("undump", "%s_lammpstrj"%(self.name))

        if self.options.restart:
            # for restart files we move xlo, ylo, zlo back to 0 so to have same origin as a cif file
            # also we modify to have unscaled coords so we can directly compute scaled coordinates WITH CIF BASIS
            inp_str += "\n# Dump last snapshot for restart\n"

            inp_str += "variable curr_lx equal lx\n"
            inp_str += "variable curr_ly equal ly\n"
            inp_str += "variable curr_lz equal lz\n"
            inp_str += "change_box all x final 0 ${curr_lx} y final 0 ${curr_ly} z final 0 ${curr_lz}\n\n"
            inp_str += "reset_timestep 0\n"
            inp_str += "%-15s %s\n"%("dump","%s_restart all atom 1 %s_restart.lammpstrj"%
                            (self.name, self.name))
            inp_str += "%-15s %s_restart scale no sort id\n"%("dump_modify",self.name)
            inp_str += "run 0\n"
            inp_str += "%-15s %s\n"%("undump", "%s_restart"%(self.name))

            # write a string that tells you how to read the dump file for this structure
            f=open("dump_restart_string.txt","w")
            f.write("read_dump %s_restart.lammpstrj %d x y z box yes"%(self.name,
                                                                       0))
            f.close()

        try:
            inp_str += "%-15s %i\n"%("unfix", shk_fix)
        except NameError:
            # no shake fix id in this input file.
            pass
        return inp_str

    def groups(self, ints):
        ints = sorted(ints)
        for k, g in itertools.groupby(enumerate(ints), lambda ix : ix[0]-ix[1]):
            yield list(map(operator.itemgetter(1), g))

    # this needs to be somewhere else.
    @profile
    def compute_molecules(self, size_cutoff=0.5):
        """Ascertain if there are molecules within the porous structure"""
        for j in nx.connected_components(self.graph):
            # return a list of nodes of connected graphs (decisions to isolate them will come later)
            # Upper limit on molecule size is 100 atoms.
            if((len(j) <= self.graph.original_size*size_cutoff) or (len(j) < 25)) and (not len(j) > 100) :
                self.molecules.append(j)

    @profile
    def cut_molecule(self, nodes):
        mgraph = self.graph.subgraph(nodes)
        self.graph.remove_nodes_from(nodes)
        indices = np.array(list(nodes))
        indices -= 1
        mgraph.coordinates = self.graph.coordinates[indices,:].copy()
        #mgraph.sorted_edge_dict = self.graph.sorted_edge_dict.copy()
        mgraph.sorted_edge_dict = {}
        mgraph.distance_matrix = self.graph.distance_matrix.copy()
        mgraph.original_size = self.graph.original_size
        for n1, n2 in mgraph.edges():
            try:
                val = self.graph.sorted_edge_dict.pop((n1, n2))
                mgraph.sorted_edge_dict.update({(n1, n2):val})
            except KeyError:
                print("something went wrong")
            try:
                val = self.graph.sorted_edge_dict.pop((n2, n1))
                mgraph.sorted_edge_dict.update({(n2,n1):val})
            except KeyError:
                print("something went wrong")
        return mgraph

def main():

    # command line parsing
    options = Options()
    sim = LammpsSimulation(options)
    cell, graph = from_CIF(options.cif_file)
    sim.set_cell(cell)
    sim.set_graph(graph)
    sim.split_graph()
    sim.assign_force_fields()
    sim.compute_simulation_size()
    sim.merge_graphs()
    if options.output_cif:
        print("CIF file requested. Exiting...")
        write_CIF(graph, cell)
        sys.exit()

    sim.write_lammps_files()

    # Additional capability to write RASPA files if requested
    if options.output_raspa:
        print("Writing RASPA files to current WD")
        classifier=1
        write_RASPA_CIF(graph, cell,classifier)
        write_RASPA_sim_files(sim,classifier)
        this_config = MDMC_config(sim)
        sim.set_MDMC_config(this_config)

if __name__ == "__main__":
    main()
