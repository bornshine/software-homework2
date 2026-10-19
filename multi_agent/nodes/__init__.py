"""各角色节点。

代码流水线：router → coder → reviewer ⇄ fixer
自由问答：  router → responder

每个节点都提供一个工厂（make_xxx_node）用于注入 LLM，
以及一个纯函数（run_xxx）便于单测直接调用。
"""

from multi_agent.nodes.coder_node import coder_node, make_coder_node, run_coder
from multi_agent.nodes.fixer_node import fixer_node, make_fixer_node, run_fixer
from multi_agent.nodes.responder_node import make_responder_node, responder_node, run_responder
from multi_agent.nodes.reviewer_node import make_reviewer_node, reviewer_node, run_reviewer
from multi_agent.nodes.router_node import make_router_node, run_router, router_node

__all__ = [
    "coder_node",
    "fixer_node",
    "make_coder_node",
    "make_fixer_node",
    "make_responder_node",
    "make_reviewer_node",
    "make_router_node",
    "responder_node",
    "reviewer_node",
    "router_node",
    "run_coder",
    "run_fixer",
    "run_responder",
    "run_reviewer",
    "run_router",
]
