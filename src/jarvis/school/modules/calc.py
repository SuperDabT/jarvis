"""Multivariable calculus (a starter: the gradient). Its visuals arrive next stage."""
from __future__ import annotations

from jarvis.school.modules.base import Module

CALC = Module(
    key="calc", name="Calculus III",
    summary="Functions of several variables: partial derivatives, gradients, optimization.",
    topics=("functions of two variables", "level curves and contour maps",
            "partial derivatives", "the gradient and directional derivatives",
            "gradient descent", "tangent planes", "Lagrange multipliers"),
    starters=("What is the gradient?", "Directional derivatives", "Gradient descent"),
)
