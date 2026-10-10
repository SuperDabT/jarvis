"""Discrete Math (COMSC-230). Its visuals arrive in the next stage."""
from __future__ import annotations

from jarvis.school.modules.base import Module

DISCRETE = Module(
    key="discrete", name="Discrete Math",
    summary="Logic, sets, functions, relations, graphs, counting and proofs.",
    courses=("COMSC-230",),
    topics=("propositional logic and truth tables", "logical equivalence", "predicates and "
            "quantifiers", "proof techniques and induction", "sets and Venn diagrams",
            "functions (injective, surjective, bijective)", "relations", "recursion and "
            "recurrences", "counting and combinatorics", "graphs and trees", "graph search"),
    starters=("Truth tables", "Proof by induction", "Sets and Venn diagrams",
              "Is this function one-to-one?", "Graph search (BFS / DFS)"),
)
