#!/usr/bin/env python3
"""Generate the figures for the writeup (doc.md and docs/).

Rounded boxes with pastel fills and saturated borders, gray labelled arrows, a baked in title.
No hyphens in any visible label (house style); arrows are drawn, not typed. Outputs into
docs/figures/.  Run:  python3 scripts/make_figures.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

OUT = Path(__file__).resolve().parent.parent / "docs" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

INK = "#1f2937"
ARROW = "#5b6470"
SLASH = "#b23b3b"
ACCENT = "#1F4E79"

# (fill, edge)
AGENT = ("#eae7f6", "#6b5ba6")
SDK = ("#d6e4f0", "#1F4E79")
DATA = ("#dcefe3", "#2e7d5b")
CHAIN = ("#fbe9d0", "#c77d2e")
VERIF = ("#f8ddcf", "#c0552b")
NEUT = ("#ececf0", "#6b7280")


def box(ax, x, y, w, h, title, lines=(), colors=SDK, title_size=13, body_size=10.5):
    fill, edge = colors
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.3,rounding_size=1.1",
                                linewidth=2, facecolor=fill, edgecolor=edge, mutation_aspect=1))
    cx = x + w / 2
    if lines:
        ax.text(cx, y + h - h * 0.30, title, ha="center", va="center",
                fontsize=title_size, fontweight="bold", color=INK)
        ax.text(cx, y + h * 0.40, "\n".join(lines), ha="center", va="center",
                fontsize=body_size, color=INK, linespacing=1.45)
    else:
        ax.text(cx, y + h / 2, title, ha="center", va="center",
                fontsize=title_size, fontweight="bold", color=INK)


def arrow(ax, p0, p1, color=ARROW, dashed=False, lw=2.0, rad=0.0):
    ax.add_patch(FancyArrowPatch(p0, p1, arrowstyle="-|>", mutation_scale=18, lw=lw,
                                 color=color, shrinkA=3, shrinkB=3,
                                 linestyle=(0, (5, 4)) if dashed else "solid",
                                 connectionstyle=f"arc3,rad={rad}"))


def label(ax, x, y, text, color=ARROW, size=10, weight="normal", ha="center"):
    ax.text(x, y, text, ha=ha, va="center", fontsize=size, color=color, weight=weight)


def title(ax, text):
    ax.set_title(text, fontsize=16, fontweight="bold", color=INK, pad=14)


def canvas(w_in, h_in, xlim, ylim):
    fig, ax = plt.subplots(figsize=(w_in, h_in))
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.axis("off")
    return fig, ax


def save(fig, name):
    fig.savefig(OUT / name, dpi=200, bbox_inches="tight", pad_inches=0.18, facecolor="white")
    plt.close(fig)
    print("wrote", OUT / name)


# ── Figure 1 — the lifecycle ─────────────────────────────────────────────────────
def fig_lifecycle():
    fig, ax = canvas(13, 7.2, (0, 100), (0, 60))
    title(ax, "The lifecycle of a promise")
    box(ax, 2, 27, 24, 17, "Provider", ["the agent and the SDK", "in one process;", "guard logs every action"], AGENT)
    box(ax, 37, 45, 28, 12, "Store  (offchain)", ["session traces + promise params"], DATA, title_size=12, body_size=10)
    box(ax, 37, 21, 28, 14, "Escrow  (onchain)", ["bond + commitments;", "slash + pay on a verdict"], CHAIN, title_size=12)
    box(ax, 73, 33, 25, 15, "Verifier", ["recompute hashes,", "run predicate, rule"], VERIF, title_size=12)
    box(ax, 73, 7, 25, 13, "User / hunter", ["challenges a", "(session, promise)"], NEUT, title_size=12)

    arrow(ax, (26, 39), (37, 49))
    label(ax, 28.5, 47.5, "trace +\nparams", size=9, ha="left")
    arrow(ax, (26, 33), (37, 28))
    label(ax, 29, 33.5, "bond +\n3 hashes", size=9, ha="left")
    arrow(ax, (65, 29), (73, 37))
    label(ax, 66.5, 35, "event", size=9, ha="left")
    arrow(ax, (73, 34), (66, 27))
    label(ax, 70, 29.5, "verdict", size=9, ha="center")
    arrow(ax, (78, 48), (63, 53), rad=0.12)
    label(ax, 73, 53.5, "fetch trace + params", size=9, ha="center")
    arrow(ax, (75, 18), (65, 25))
    label(ax, 67, 18.5, "challenge + bond", size=9, ha="left")
    arrow(ax, (37, 24), (26, 30), color=SLASH, dashed=True)
    label(ax, 29, 25.5, "slash", size=9, color=SLASH, ha="left")
    save(fig, "proto_lifecycle.png")


# ── Figure 2 — a promise is one function configured by params ─────────────────────
def fig_predicate():
    fig, ax = canvas(13, 6.6, (0, 100), (0, 56))
    title(ax, "A promise is one audited function, configured by params")
    box(ax, 2, 33, 31, 14, "Provider A", ["destructive_tools", "= [delete_file]"], AGENT, title_size=12, body_size=10.5)
    box(ax, 2, 11, 31, 14, "Provider B", ["destructive_tools", "= [rm, shred]"], AGENT, title_size=12, body_size=10.5)
    # AAP1 drawn by hand so the title sits well clear of the body
    fill, edge = SDK
    ax.add_patch(FancyBboxPatch((41, 14), 31, 26, boxstyle="round,pad=0.3,rounding_size=1.1",
                                linewidth=2, facecolor=fill, edgecolor=edge))
    ax.text(56.5, 35, "AAP1", ha="center", va="center", fontsize=15, fontweight="bold", color=INK)
    ax.text(56.5, 25, "no destructive action\nwithout consent\n\none shared function,\nparams pick the tools",
            ha="center", va="center", fontsize=10.5, color=INK, linespacing=1.5)
    box(ax, 80, 21, 18, 13, "Verdict", ["over each agent's", "own trace"], DATA, title_size=12, body_size=10)

    arrow(ax, (33, 40), (41, 32)); label(ax, 36, 38, "params", size=9, ha="left")
    arrow(ax, (33, 18), (41, 25)); label(ax, 36, 19.5, "params", size=9, ha="left")
    arrow(ax, (72, 27), (80, 27))
    ax.text(50, 4, "Same function, different params for each provider's tools.",
            ha="center", va="center", fontsize=10.5, style="italic", color=ARROW)
    save(fig, "proto_predicate.png")


# ── (kept for docs/INTERNALS) the SDK chokepoint ─────────────────────────────────
def fig_sdk():
    fig, ax = canvas(13, 6.6, (0, 100), (0, 56))
    title(ax, "The SDK — one chokepoint, three steps")
    box(ax, 1, 33, 22, 16, "Agent framework", ["Claude Agent SDK,", "OpenAI, Hermes, ...", "(differs per provider)"], AGENT, title_size=12, body_size=10)
    box(ax, 28, 35, 19, 12, "wire to guard", ["the framework's", "tool dispatch point"], NEUT, title_size=12)
    box(ax, 52, 33, 21, 16, "guard(tool, args, run)", ["1. run the tool", "2. append the record", "3. ship to the store"], SDK, title_size=12, body_size=10)
    box(ax, 79, 34, 20, 14, "ActionRecord", ["seq, tool, args,", "result, ts", "(uniform)"], DATA, title_size=12, body_size=10)
    arrow(ax, (23, 41), (28, 41))
    arrow(ax, (47, 41), (52, 41))
    arrow(ax, (73, 41), (79, 41)); label(ax, 76, 43.2, "append", size=9)
    label(ax, 25.5, 43.0, "call", size=9); label(ax, 49.5, 43.0, "call", size=9)
    ax.text(50, 3.0, "Past guard, every action is the same record, whatever framework produced it.",
            ha="center", va="center", fontsize=9.5, style="italic", color=ARROW)
    save(fig, "proto_sdk.png")


# ── Figure 3 — binding ───────────────────────────────────────────────────────────
def fig_binding():
    fig, ax = canvas(13, 7.6, (0, 100), (0, 64))
    title(ax, "Binding — hashes onchain, data offchain, code shared")
    box(ax, 1, 47, 41, 13, "Onchain (Escrow)", [
        "predicateHash   paramsHash   traceHash"], CHAIN, title_size=12, body_size=10.5)
    label(ax, 21.5, 45.2, "32 byte commitments, the root of trust", size=9, color=ACCENT)
    box(ax, 1, 26, 41, 13, "Store (offchain)", [
        "predicate_id (a pointer),", "params, trace"], DATA, title_size=12, body_size=10.5)
    label(ax, 21.5, 24.2, "data, available but not trusted", size=9, color="#2e7d5b")
    box(ax, 1, 5, 41, 13, "Catalog (shared code)", [
        "the audited predicate sources,", "imported by SDK and verifier"], AGENT, title_size=12, body_size=10.5)

    box(ax, 57, 22, 41, 23, "Verifier", [
        "resolve predicate_id, then",
        "recompute and compare",
        "H(source) =? predicateHash",
        "H(params) =? paramsHash",
        "H(trace)  =? traceHash",
        "mismatch  →  tamper, refuse"], VERIF, title_size=12.5, body_size=10)

    arrow(ax, (42, 52), (57, 40))
    label(ax, 49, 49.5, "read commitments", size=9)
    arrow(ax, (42, 32), (57, 33))
    label(ax, 49.5, 34.6, "fetch id, params, trace", size=9)
    arrow(ax, (42, 12), (57, 26))
    label(ax, 48, 16.5, "import predicate", size=9, ha="left")
    arrow(ax, (77, 22), (77, 13))
    box(ax, 58, 2, 39, 9, "Run the predicate  →  verdict", ["the same function the SDK used"], SDK, title_size=11.5, body_size=10)
    save(fig, "proto_binding.png")


# ── Figure 4 — Hermes ────────────────────────────────────────────────────────────
def fig_hermes_normalization():
    fig, ax = canvas(13, 8.2, (0, 100), (0, 72))
    title(ax, "Hermes normalizes providers; the SDK binds below the model layer")

    provs = [(3, "Anthropic", "tool_use"), (27, "OpenAI", "function call"),
             (51, "OpenRouter", "200+ models"), (75, "your endpoint", "OpenAI compatible")]
    for x, name, fmt in provs:
        box(ax, x, 61, 22, 8, name, [fmt], NEUT, title_size=11, body_size=9)

    box(ax, 6, 47, 88, 9, "Hermes turns every model's format into one internal tool call (name, args)",
        [], AGENT, title_size=12)
    for (x, *_), tx in zip(provs, (24, 42, 58, 76)):
        arrow(ax, (x + 11, 61), (tx, 56))

    ax.plot([6, 94], [43, 43], linestyle=(0, (5, 4)), color=ARROW, lw=1.4)
    ax.text(50, 43, "  below here it is provider agnostic, the same (name, args) whatever the model  ",
            ha="center", va="center", fontsize=9.5, style="italic", color=ACCENT,
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none"))

    box(ax, 22, 28, 50, 9, "one tool dispatch point", ["every tool call Hermes makes passes through here"], SDK, title_size=12, body_size=10)
    arrow(ax, (50, 47), (50, 37))
    box(ax, 80, 28, 18, 9, "the SDK", ["binds here"], CHAIN, title_size=11, body_size=10)
    arrow(ax, (80, 32.5), (72, 32.5), color=ACCENT)
    arrow(ax, (50, 28), (50, 20))
    box(ax, 30, 11, 40, 8, "the tool runs, the real effect", [], DATA, title_size=11)
    ax.text(50, 4.5, "scope-only middleware view; native authorization capture also uses a pinned Hermes patch.",
            ha="center", va="center", fontsize=9.5, style="italic", color=ARROW)
    save(fig, "proto_hermes_seam.png")


if __name__ == "__main__":
    fig_lifecycle()
    fig_predicate()
    fig_sdk()
    fig_binding()
    fig_hermes_normalization()
