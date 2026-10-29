"""csdview —— Cocos Studio .csd 檢視與渲染工具。

典型用法：

    from csdview import render_file
    doc, result = render_file("Deal.csd", frame=30, animation="deal")
    result.image.save("frame30.png")
"""
from .anim import FPS, AnimationState
from .assets import AssetLibrary, find_project_root, library_for
from .model import Document, Node
from .parser import parse
from .render import RenderOptions, Renderer, RenderResult, render_file

__version__ = "1.0.0"

__all__ = [
    "FPS", "AnimationState", "AssetLibrary", "Document", "Node",
    "RenderOptions", "Renderer", "RenderResult",
    "find_project_root", "library_for", "parse", "render_file",
]
