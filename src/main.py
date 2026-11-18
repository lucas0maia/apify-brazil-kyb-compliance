"""Apify Actor entry point supporting Batch runs and Standby HTTP/MCP modes."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from aiohttp import web
from apify import Actor

from .kyb_client import run_kyb_query

MCP_TOOLS = [
    {
        "name": "screen_brazil_entities",
        "description": (
            "Run Brazilian KYB & ESG due diligence screening on CNPJs or CPFs across "
            "Receita Federal QSA, CGU CEIS, CGU CNEP, CGU CEPIM, MTE Slave Labor Dirty List, "
            "and optional IBAMA Environmental Embargoes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "documents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Brazilian CNPJs (14 digits) or CPFs (11 digits) to screen.",
                },
                "includeReceita": {"type": "boolean", "default": True},
                "includeIbama": {"type": "boolean", "default": False},
            },
            "required": ["documents"],
        },
    },
    {
        "name": "search_brazil_sanctions",
        "description": (
            "Search Brazilian official sanctions & ESG blacklists "
            "(ceis_sanctions, cnep_punishments, cepim_impediments, slave_labor_registry, ibama_embargoes)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": [
                        "ceis_sanctions",
                        "cnep_punishments",
                        "cepim_impediments",
                        "slave_labor_registry",
                        "ibama_embargoes",
                    ],
                },
                "query": {"type": "string"},
                "uf": {"type": "string"},
                "maxResults": {"type": "integer", "default": 20},
            },
            "required": ["mode"],
        },
    },
]


async def _handle_mcp_rpc(payload: dict[str, Any]) -> dict[str, Any]:
    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "brazil-kyb-compliance", "version": "1.0.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "screen_brazil_entities":
            actor_input = {"mode": "screen_entities", **args}
        elif name == "search_brazil_sanctions":
            actor_input = dict(args)
        else:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }

        records, event_name = await run_kyb_query(actor_input)
        await _charge_records(event_name, len(records))
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(records, ensure_ascii=False)}]
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"Unsupported MCP method: {method}"},
    }


async def _charge_records(event_name: str, count: int) -> int:
    """Charge PPE events gracefully while respecting Apify spending caps."""
    if count <= 0:
        return 0
    max_items_env = os.environ.get("ACTOR_MAX_PAID_DATASET_ITEMS")
    allowed = count
    if max_items_env and max_items_env.isdigit():
        allowed = min(count, int(max_items_env))
    if allowed <= 0:
        return 0
    try:
        charge_res = await Actor.charge(event_name=event_name, count=allowed)
        if hasattr(charge_res, "event_charge_limit_reached") and charge_res.event_charge_limit_reached:
            Actor.log.warning("Pay-per-event spending limit reached on %s.", event_name)
    except Exception as exc:
        Actor.log.warning("Charge skipped in local/non-monetized mode (%s): %s", event_name, exc)
    return allowed


async def _run_standby_server() -> None:
    port = int(os.environ.get("ACTOR_STANDBY_PORT", "8080"))

    async def handle_root(request: web.Request) -> web.Response:
        if request.headers.get("x-apify-container-server-readiness-probe"):
            return web.Response(text="OK", status=200)
        mode = request.query.get("mode", "screen_entities")
        docs = [d for d in request.query.get("documents", "").split(",") if d.strip()]
        query = request.query.get("query", "")
        uf = request.query.get("uf", "")
        max_results = int(request.query.get("maxResults", "25"))
        records, event_name = await run_kyb_query(
            {
                "mode": mode,
                "documents": docs or (["33000167000101"] if not query and mode == "screen_entities" else []),
                "query": query,
                "uf": uf,
                "maxResults": max_results,
            }
        )
        await _charge_records(event_name, len(records))
        return web.json_response({"count": len(records), "items": records})

    async def handle_post(request: web.Request) -> web.Response:
        payload = await request.json()
        if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
            res = await _handle_mcp_rpc(payload)
            return web.json_response(res)
        records, event_name = await run_kyb_query(payload if isinstance(payload, dict) else {})
        await _charge_records(event_name, len(records))
        return web.json_response({"count": len(records), "items": records})

    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_post("/", handle_post)
    app.router.add_post("/mcp", handle_post)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    Actor.log.info("Brazil KYB Compliance Standby HTTP/MCP server listening on port %s", port)
    await asyncio.Event().wait()


async def main() -> None:
    async with Actor:
        if os.environ.get("APIFY_META_ORIGIN") == "STANDBY":
            await _run_standby_server()
            return

        actor_input = await Actor.get_input() or {}
        records, event_name = await run_kyb_query(actor_input)
        allowed = await _charge_records(event_name, len(records))
        to_push = records[:allowed] if allowed else []
        if to_push:
            await Actor.push_data(to_push)
        Actor.log.info("Delivered %d records (%s).", len(to_push), event_name)


if __name__ == "__main__":
    asyncio.run(main())
