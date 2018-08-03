from pathlib import Path
from collections import Counter, defaultdict

import yaml
import numpy as np
import pandas as pd

from knockin import experiment
from sequencing import fastq
from sequencing.utilities import group_by

from . import collapse
from .collapse_cython import hamming_distance_matrix, register_corrections

class UMI_Outcome(object):
    columns = [
        'cell_BC',
        'UMI',
        'num_reads',
        'category',
        'subcategory',
        'details',
        'query_name',
    ]

    def __init__(self, cell_BC, UMI, num_reads, category, subcategory, details, query_name):
        self.cell_BC = cell_BC
        self.UMI = UMI
        self.num_reads = int(num_reads)
        self.category = category
        self.subcategory = subcategory
        self.details = details
        self.query_name = query_name

    @classmethod
    def from_line(cls, line):
        fields = line.strip().split('\t')
        return cls(**dict(zip(UMI_Outcome.columns, fields)))
    
    @property
    def outcome(self):
        return (self.category, self.subcategory, self.details)        

    def __str__(self):
        row = [str(getattr(self, k)) for k in UMI_Outcome.columns]
        return '\t'.join(row)

class Pooled_UMI_Outcome(object):
    columns = [
        'UMI',
        'cluster_id',
        'num_reads',
        'category',
        'subcategory',
        'details',
    ]

    def __init__(self, UMI, cluster_id, num_reads, category, subcategory, details):
        self.UMI = UMI
        self.cluster_id = cluster_id
        self.num_reads = int(num_reads)
        self.category = category
        self.subcategory = subcategory
        self.details = details

    @classmethod
    def from_line(cls, line):
        fields = line.strip().split('\t')
        return cls(**dict(zip(Pooled_UMI_Outcome.columns, fields)))
    
    @property
    def outcome(self):
        return (self.category, self.subcategory, self.details)        

    def __str__(self):
        row = [str(getattr(self, k)) for k in Pooled_UMI_Outcome.columns]
        return '\t'.join(row)

def load_UMI_outcomes(fn, pooled=True):
    if pooled:
        Outcome = Pooled_UMI_Outcome
    else:
        Outcome = UMI_Outcome

    UMI_outcomes = []
    for line in fn.open():
        UMI_outcome = Outcome.from_line(line)
        UMI_outcomes.append(UMI_outcome)

    return UMI_outcomes

def load_cell_outcomes(fn):
    cell_outcomes = []
    for line in fn.open():
        cell_outcome = cell_Outcome.from_line(line)
        cell_outcomes.append(cell_outcome)
    return cell_outcomes

class cell_Outcome(object):
    columns = [
        'cell_BC',
        'num_UMIs',
        'num_reads',
        'category',
        'subcategory',
        'details',
        'query_name',
    ]

    def __init__(self, cell_BC, num_UMIs, num_reads, category, subcategory, details, query_name):
        self.cell_BC = cell_BC
        self.num_UMIs = num_UMIs
        self.num_reads = num_reads
        self.category = category
        self.subcategory = subcategory
        self.details = details
        self.query_name = query_name

    @property
    def outcome(self):
        return (self.category, self.subcategory, self.details)        
    
    @classmethod
    def from_line(cls, line):
        fields = line.strip().split('\t')
        return cls(**dict(zip(cell_Outcome.columns, fields)))
    
    def __str__(self):
        row = [str(getattr(self, k)) for k in cell_Outcome.columns]
        return '\t'.join(row)

def collapse_UMI_outcomes(input_fn):
    def is_relevant(outcome):
        return (outcome.category != 'bad sequence' and
                outcome.outcome != ('no indel', 'other', 'ambiguous')
               )

    UMI_outcomes = [o for o in load_UMI_outcomes(input_fn, False) if is_relevant(o)]
    UMI_outcomes = sorted(UMI_outcomes, key=lambda u: (u.cell_BC, u.UMI))

    most_abundant_outcomes = []

    for cell_BC, cell_UMIs in group_by(UMI_outcomes, lambda u: u.cell_BC):
        for outcome, outcome_UMIs in group_by(cell_UMIs, lambda u: u.outcome, sort=True):
            error_correct_outcome_UMIs(outcome_UMIs)

        for UMI, UMI_outcomes in group_by(cell_UMIs, lambda u: u.UMI, sort=True):
            relevant_outcomes = set(u.outcome for u in UMI_outcomes)

            collapsed_outcomes = []
            for outcome in relevant_outcomes:
                relevant = [u for u in UMI_outcomes if u.outcome == outcome]
                UMI_outcome = max(relevant, key=lambda u: u.num_reads)
                UMI_outcome.num_reads = sum(u.num_reads for u in relevant)

                collapsed_outcomes.append(UMI_outcome)
            
            max_count = max(u.num_reads for u in collapsed_outcomes)
            has_max_count = [u for u in collapsed_outcomes if u.num_reads == max_count]
            most_abundant_outcomes.extend(has_max_count)

    return most_abundant_outcomes

def collapse_pooled_UMI_outcomes(input_fn):
    def is_relevant(outcome):
        return (outcome.category != 'bad sequence' and
                outcome.outcome != ('no indel', 'other', 'ambiguous')
               )

    all_outcomes = [o for o in load_UMI_outcomes(input_fn, True) if is_relevant(o)]
    all_outcomes = sorted(all_outcomes, key=lambda u: (u.UMI, u.cluster_id))

    #most_abundant_outcomes = []
    collapsed_outcomes = []

    for UMI, UMI_outcomes in group_by(all_outcomes, lambda u: u.UMI):
        observed = set(u.outcome for u in UMI_outcomes)

        #collapsed_outcomes = []
        for outcome in observed:
            relevant = [u for u in UMI_outcomes if u.outcome == outcome]
            representative = max(relevant, key=lambda u: u.num_reads)
            representative.num_reads = sum(u.num_reads for u in relevant)

            collapsed_outcomes.append(representative)

    collapsed_outcomes = sorted(collapsed_outcomes, key=lambda u: (u.UMI, u.cluster_id))
    return collapsed_outcomes
        
    #    max_count = max(u.num_reads for u in collapsed_outcomes)
    #    has_max_count = [u for u in collapsed_outcomes if u.num_reads == max_count]
    #    most_abundant_outcomes.extend(has_max_count)

    #return most_abundant_outcomes

def error_correct_outcome_UMIs(outcome_group, max_UMI_distance=1):
    # sort UMIs in descending order by number of occurrences.
    UMI_read_counts = Counter()
    for outcome in outcome_group:
        UMI_read_counts[outcome.UMI] += outcome.num_reads
    UMIs = [UMI for UMI, read_count in UMI_read_counts.most_common()]

    ds = hamming_distance_matrix(UMIs)

    corrections = register_corrections(ds, max_UMI_distance, UMIs)

    for outcome in outcome_group:
        correct_to = corrections.get(outcome.UMI)
        if correct_to:
            outcome.UMI = correct_to
    
    return outcome_group

def collapse_cell_outcomes(UMI_fn):
    cell_outcomes = []
    
    def is_relevant(outcome):
        return (outcome.category != 'bad sequence' and
                outcome.category != 'endogenous' and
                outcome.outcome != ('no indel', 'other', 'ambiguous')
               )
    
    UMI_outcomes = [o for o in load_UMI_outcomes(UMI_fn) if is_relevant(o)]
    UMI_outcomes = sorted(UMI_outcomes, key=lambda u: u.cell_BC)

    for cell_BC, cell_UMIs in group_by(UMI_outcomes, lambda u: u.cell_BC):
        outcomes = set(u.outcome for u in cell_UMIs)
 
        for outcome in outcomes:
            category, subcategory, details = outcome
            relevant = [u for u in cell_UMIs if u.outcome == outcome]
            num_UMIs = len(relevant)
            num_reads = sum(u.num_reads for u in relevant)
            name = max(relevant, key=lambda u: u.num_reads).query_name
            cell_outcome = cell_Outcome(cell_BC, num_UMIs, num_reads, category, subcategory, details, name)
            cell_outcomes.append(cell_outcome)
            
    return cell_outcomes

def filter_coherent_cells(cell_fn):
    cell_table = pd.read_table(cell_fn, header=None, names=cell_Outcome.columns)
    cell_table['reads_per_UMI'] = cell_table['num_reads'] / cell_table['num_UMIs']
    filtered = cell_table.query('reads_per_UMI >= 5 and num_UMIs >= 5')
    # Only retain cells that only have 1 outcome that passes filtering.
    good_cell_BCs = filtered['cell_BC'].value_counts().loc[lambda x: x == 1].index
    coherent_cells = filtered.query('cell_BC in @good_cell_BCs').set_index('cell_BC')

    return coherent_cells

def load_cell_tables(base_dir, group):
    exps = experiment.get_all_experiments(base_dir, {'group': group})
    fns = [exp.fns['cell_outcomes'] for exp in exps]
    tables = []
    for exp in sorted(exps, key=lambda e: e.name):
        table = pd.read_table(exp.fns['cell_outcomes'], header=None, names=cell_Outcome.columns)
        table['guide'] = exp.name
        tables.append(table)

    cell_table = pd.concat(tables, ignore_index=True)
    return cell_table