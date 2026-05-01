"""
Runs as a subprocess. Initializes bittensor synchronously (no asyncio conflicts)
and serves JSON requests from stdin, returning JSON responses to stdout.
"""
import sys
import json
import logging

# Suppress all logging to stdout — keep stdout clean for JSON comms
logging.disable(logging.CRITICAL)

# Suppress loguru (used by bittensor) to stderr only
try:
    from loguru import logger as _loguru
    _loguru.remove()
    _loguru.add(sys.stderr, level="ERROR")
except Exception:
    pass


def get_subnet_detail(sub, netuid: int) -> dict:
    try:
        subnets = sub.get_all_subnets_info()
        info = next((s for s in subnets if s.netuid == netuid), None)
    except Exception as e:
        return {"error": f"get_all_subnets_info failed: {e}"}

    result = {"netuid": netuid}

    if info:
        try:
            result["neurons"] = getattr(info, "subnetwork_n", None)
            result["max_neurons"] = getattr(info, "max_n", None)
            result["tempo"] = getattr(info, "tempo", None)
            result["immunity_period"] = getattr(info, "immunity_period", None)
            burn = getattr(info, "burn", None)
            result["reg_cost_tao"] = float(burn.tao) if burn and hasattr(burn, "tao") else None
            ev = getattr(info, "emission_value", None)
            result["emission_value"] = float(ev) if ev is not None else None
        except Exception as e:
            result["info_error"] = str(e)

    # Query hyperparameters individually via substrate storage (bypasses broken codec)
    hp_items = {
        "kappa": ("SubtensorModule", "Kappa"),
        "rho": ("SubtensorModule", "Rho"),
        "min_allowed_weights": ("SubtensorModule", "MinAllowedWeights"),
        "max_weight_limit": ("SubtensorModule", "MaxWeightsLimit"),
        "immunity_period": ("SubtensorModule", "ImmunityPeriod"),
        "max_validators": ("SubtensorModule", "MaxValidatorsPerSubnet"),
        "weights_rate_limit": ("SubtensorModule", "WeightsRateLimit"),
        "adjustment_interval": ("SubtensorModule", "AdjustmentInterval"),
        "bonds_moving_avg": ("SubtensorModule", "BondsMovingAverage"),
        "liquid_alpha_enabled": ("SubtensorModule", "LiquidAlphaOn"),
    }
    for key, (module, storage) in hp_items.items():
        try:
            val = sub.substrate.query(module, storage, [netuid])
            if val is not None:
                result[key] = int(val.value) if hasattr(val, "value") else int(val)
        except Exception:
            pass

    return result


def _query_vec(sub, module, name, params):
    try:
        r = sub.substrate.query(module, name, params)
        if r is None:
            return []
        v = r.value if hasattr(r, "value") else r
        return list(v) if v is not None else []
    except Exception as e:
        print(f"DEBUG _query_vec {module}.{name}: {e}", file=sys.stderr, flush=True)
        return []


def list_subtensor_storage(sub) -> dict:
    try:
        meta = sub.substrate.get_metadata()
        modules = meta.value[1]["V14"]["pallets"] if "V14" in str(meta.value) else []
        for m in modules:
            if m.get("name") == "SubtensorModule":
                storage = m.get("storage", {})
                items = storage.get("entries", [])
                return {"storage_keys": [i["name"] for i in items]}
        return {"error": "SubtensorModule not found in metadata"}
    except Exception as e:
        return {"error": str(e)}


def _metagraph_from_full(sub, netuid: int) -> dict:
    """Fetch full NeuronInfo (includes rank/trust) via raw RPC, bypassing substrate-interface decoder."""
    try:
        from bittensor.core.chain_data import NeuronInfo
    except ImportError:
        from bittensor import NeuronInfo

    param_hex = "0x" + netuid.to_bytes(2, "little").hex()
    result = sub.substrate.rpc_request("state_call", ["NeuronInfoRuntimeApi_get_neurons", param_hex])
    hex_bytes = result.get("result") or "0x"
    if hex_bytes in ("0x", ""):
        raise ValueError("empty get_neurons result")
    raw = bytes.fromhex(hex_bytes[2:] if hex_bytes.startswith("0x") else hex_bytes)
    neurons_full = NeuronInfo.list_from_vec_u8(raw)
    if not neurons_full:
        raise ValueError("no neurons returned from get_neurons")

    neurons = []
    for n in neurons_full:
        hk = getattr(n, "hotkey", "") or ""
        stake_val = 0.0
        try:
            ts = getattr(n, "total_stake", None)
            if ts is not None:
                stake_val = float(ts.tao) if hasattr(ts, "tao") else float(ts)
        except Exception:
            pass
        em = 0.0
        try:
            raw_em = getattr(n, "emission", 0)
            em = float(raw_em.tao) if hasattr(raw_em, "tao") else float(raw_em)
            if em > 100:
                em /= 1e9
        except Exception:
            pass
        neurons.append({
            "uid": int(getattr(n, "uid", 0)),
            "hotkey": hk[:16] + "..." if len(hk) > 16 else hk,
            "stake": round(stake_val, 4),
            "rank": round(float(getattr(n, "rank", 0) or 0), 6),
            "trust": round(float(getattr(n, "trust", 0) or 0), 6),
            "consensus": round(float(getattr(n, "consensus", 0) or 0), 6),
            "incentive": round(float(getattr(n, "incentive", 0) or 0), 6),
            "emission_tao": round(em, 6),
            "dividends": round(float(getattr(n, "dividends", 0) or 0), 6),
            "validator_trust": round(float(getattr(n, "validator_trust", 0) or 0), 6),
            "validator_permit": bool(getattr(n, "validator_permit", False)),
        })

    top_validators = sorted(
        [x for x in neurons if x["validator_permit"]],
        key=lambda x: (x["stake"], x["dividends"]), reverse=True
    )[:10]
    miners = [x for x in neurons if not x["validator_permit"]]
    top_miners = sorted(miners, key=lambda x: (x["incentive"], x["consensus"], x["emission_tao"]), reverse=True)[:10]
    all_miners = [{"uid": x["uid"], "hotkey": x["hotkey"], "incentive": x["incentive"],
                   "consensus": x["consensus"], "emission_tao": x["emission_tao"]} for x in miners]

    return {
        "netuid": netuid,
        "n": len(neurons),
        "total_stake_tao": round(sum(x["stake"] for x in neurons), 4),
        "total_emission_tao": round(sum(x["emission_tao"] for x in neurons), 6),
        "top_validators": top_validators,
        "top_miners": top_miners,
        "all_miners": all_miners,
    }


def _fetch_neurons_lite(sub, netuid: int):
    """Call NeuronInfoRuntimeApi directly, bypassing substrate-interface's broken <Bytes> decoder."""
    try:
        from bittensor.core.chain_data import NeuronInfoLite
    except ImportError:
        from bittensor import NeuronInfoLite

    param_hex = "0x" + netuid.to_bytes(2, "little").hex()
    result = sub.substrate.rpc_request("state_call", ["NeuronInfoRuntimeApi_get_neurons_lite", param_hex])
    hex_bytes = result.get("result") or "0x"
    if hex_bytes in ("0x", ""):
        return []
    raw = bytes.fromhex(hex_bytes[2:] if hex_bytes.startswith("0x") else hex_bytes)
    return NeuronInfoLite.list_from_vec_u8(raw)


def _metagraph_from_neurons_lite(sub, netuid: int) -> dict:
    try:
        neurons_lite = _fetch_neurons_lite(sub, netuid)
    except Exception:
        neurons_lite = sub.neurons_lite(netuid=netuid)
    if not neurons_lite:
        raise ValueError("empty neurons_lite result")

    neurons = []
    for n in neurons_lite:
        hk = getattr(n, "hotkey", "") or ""
        stake_val = 0.0
        try:
            ts = getattr(n, "total_stake", None)
            if ts is not None:
                stake_val = float(ts.tao) if hasattr(ts, "tao") else float(ts)
        except Exception:
            pass
        if stake_val == 0.0:
            try:
                stake_dict = getattr(n, "stake", {})
                if isinstance(stake_dict, dict):
                    stake_val = sum(
                        float(v.tao) if hasattr(v, "tao") else float(v)
                        for v in stake_dict.values()
                    )
            except Exception:
                pass
        em = 0.0
        try:
            raw = getattr(n, "emission", 0)
            em = float(raw.tao) if hasattr(raw, "tao") else float(raw)
            if em > 100:
                em /= 1e9
        except Exception:
            pass
        neurons.append({
            "uid": int(getattr(n, "uid", 0)),
            "stake": round(stake_val, 4),
            "rank": round(float(getattr(n, "rank", 0) or 0), 6),
            "trust": round(float(getattr(n, "trust", 0) or 0), 6),
            "consensus": round(float(getattr(n, "consensus", 0) or 0), 6),
            "validator_trust": round(float(getattr(n, "validator_trust", 0) or 0), 6),
            "emission_tao": round(em, 6),
            "incentive": round(float(getattr(n, "incentive", 0) or 0), 6),
            "dividends": round(float(getattr(n, "dividends", 0) or 0), 6),
            "validator_permit": bool(getattr(n, "validator_permit", False)),
            "hotkey": hk[:16] + "..." if len(hk) > 16 else hk,
        })

    top_validators = sorted(
        [x for x in neurons if x["validator_permit"]],
        key=lambda x: (x["stake"], x["dividends"]), reverse=True
    )[:10]
    miners = [x for x in neurons if not x["validator_permit"]]
    top_miners = sorted(miners, key=lambda x: (x["incentive"], x["consensus"], x["emission_tao"]), reverse=True)[:10]
    all_miners = [{"uid": x["uid"], "hotkey": x["hotkey"], "incentive": x["incentive"],
                   "consensus": x["consensus"], "emission_tao": x["emission_tao"]} for x in miners]

    return {
        "netuid": netuid,
        "n": len(neurons),
        "total_stake_tao": round(sum(x["stake"] for x in neurons), 4),
        "total_emission_tao": round(sum(x["emission_tao"] for x in neurons), 6),
        "top_validators": top_validators,
        "top_miners": top_miners,
        "all_miners": all_miners,
    }


def _metagraph_from_storage(sub, netuid: int) -> dict:
    RAO = 1e9
    U16_MAX = 65535.0

    emission  = _query_vec(sub, "SubtensorModule", "Emission", [netuid])
    incentive = _query_vec(sub, "SubtensorModule", "Incentive", [netuid])
    dividends = _query_vec(sub, "SubtensorModule", "Dividends", [netuid])
    vp        = _query_vec(sub, "SubtensorModule", "ValidatorPermit", [netuid])
    consensus = _query_vec(sub, "SubtensorModule", "Consensus", [netuid])
    vtrust    = _query_vec(sub, "SubtensorModule", "ValidatorTrust", [netuid])
    n = len(emission) or len(incentive) or len(dividends)
    if n == 0:
        raise ValueError("no storage vectors returned")

    # uid -> hotkey SS58 string
    uid_to_hk = {}
    try:
        for uid_key, hk_val in sub.substrate.query_map("SubtensorModule", "Keys", [netuid]):
            uid = int(str(uid_key))
            hk = hk_val.value if hasattr(hk_val, "value") else hk_val
            uid_to_hk[uid] = str(hk)
    except Exception:
        pass

    # hotkey -> stake TAO — query individually using the same key format from Keys
    # (bulk query_map has SS58 format mismatch; individual queries use the exact string)
    hk_to_stake = {}
    for hk_str in set(uid_to_hk.values()):
        try:
            val = sub.substrate.query("SubtensorModule", "TotalHotkeyStake", [hk_str])
            if val is not None:
                rao = val.value if hasattr(val, "value") else int(val)
                hk_to_stake[hk_str] = float(rao) / RAO
        except Exception:
            pass

    def safe_tao(lst, i):
        try: return round(float(lst[i]) / RAO, 6)
        except Exception: return 0.0

    def safe_frac(lst, i):
        try: return round(float(lst[i]) / U16_MAX, 6)
        except Exception: return 0.0

    neurons = []
    for uid in range(n):
        hk_str = uid_to_hk.get(uid, "")
        neurons.append({
            "uid": uid,
            "stake": round(hk_to_stake.get(hk_str, 0.0), 4),
            "rank": None,
            "trust": None,
            "consensus": safe_frac(consensus, uid) if consensus else None,
            "validator_trust": safe_frac(vtrust, uid) if vtrust else None,
            "emission_tao": safe_tao(emission, uid),
            "incentive": safe_frac(incentive, uid),
            "dividends": safe_frac(dividends, uid),
            "validator_permit": bool(vp[uid]) if uid < len(vp) else False,
            "hotkey": hk_str[:16] + "..." if len(hk_str) > 16 else hk_str,
        })

    top_validators = sorted(
        [x for x in neurons if x["validator_permit"]],
        key=lambda x: (x["stake"], x["dividends"]), reverse=True
    )[:10]
    miners = [x for x in neurons if not x["validator_permit"]]
    top_miners = sorted(miners, key=lambda x: (x["incentive"], x["consensus"], x["emission_tao"]), reverse=True)[:10]
    all_miners = [{"uid": x["uid"], "hotkey": x["hotkey"], "incentive": x["incentive"],
                   "consensus": x["consensus"], "emission_tao": x["emission_tao"]} for x in miners]

    return {
        "netuid": netuid,
        "n": n,
        "total_stake_tao": round(sum(x["stake"] for x in neurons), 4),
        "total_emission_tao": round(sum(x["emission_tao"] for x in neurons), 6),
        "top_validators": top_validators,
        "top_miners": top_miners,
        "all_miners": all_miners,
        "partial": True,
    }


def get_metagraph_summary(sub, netuid: int) -> dict:
    try:
        return _metagraph_from_neurons_lite(sub, netuid)
    except Exception as e:
        print(f"DEBUG neurons_lite SN{netuid}: {e}", file=sys.stderr, flush=True)
    try:
        return _metagraph_from_full(sub, netuid)
    except Exception as e:
        print(f"DEBUG metagraph_full SN{netuid}: {e}", file=sys.stderr, flush=True)
    try:
        return _metagraph_from_storage(sub, netuid)
    except Exception as e:
        return {"error": f"metagraph failed: {e}"}


def _get_alpha_price(s, sub) -> float | None:
    """Try to extract alpha token price (in TAO) from a SubnetInfo object."""
    try:
        # dTAO: price = tao_in_pool / alpha_outstanding
        for tao_attr in ("tao_in", "alpha_in", "subnet_tao"):
            alpha_in = getattr(s, tao_attr, None)
            if alpha_in is not None:
                break
        for alpha_attr in ("alpha_out", "outstanding_alpha", "alpha_outstanding"):
            alpha_out = getattr(s, alpha_attr, None)
            if alpha_out is not None:
                break
        if alpha_in is not None and alpha_out is not None:
            ai = float(alpha_in.tao) if hasattr(alpha_in, "tao") else float(alpha_in)
            ao = float(alpha_out.tao) if hasattr(alpha_out, "tao") else float(alpha_out)
            if ao > 0:
                return round(ai / ao, 8)
    except Exception:
        pass
    # Fallback: query storage directly
    try:
        netuid = getattr(s, "netuid", None)
        if netuid is None:
            return None
        ai_raw = sub.substrate.query("SubtensorModule", "SubnetAlphaIn", [netuid])
        ao_raw = sub.substrate.query("SubtensorModule", "SubnetAlphaOut", [netuid])
        if ai_raw and ao_raw:
            ai = float(ai_raw.value) / 1e9
            ao = float(ao_raw.value) / 1e9
            if ao > 0:
                return round(ai / ao, 8)
    except Exception:
        pass
    return None


def get_all_subnets(sub) -> dict:
    try:
        subnets = sub.get_all_subnets_info()
    except Exception as e:
        return {"error": str(e)}

    results = []
    for s in subnets:
        try:
            burn = getattr(s, "burn", None)
            ev = getattr(s, "emission_value", None)
            results.append({
                "netuid": s.netuid,
                "neurons": getattr(s, "subnetwork_n", None),
                "max_neurons": getattr(s, "max_n", None),
                "reg_cost_tao": float(burn.tao) if burn and hasattr(burn, "tao") else None,
                "emission_value": float(ev) if ev is not None else None,
                "tempo": getattr(s, "tempo", None),
                "immunity_period": getattr(s, "immunity_period", None),
                "alpha_price_tao": _get_alpha_price(s, sub),
            })
        except Exception:
            results.append({"netuid": getattr(s, "netuid", "?"), "error": "parse failed"})

    return {"subnets": results}


def get_weights(sub, netuid: int) -> dict:
    U16_MAX = 65535.0
    weights_map = {}
    try:
        for uid_key, weights_val in sub.substrate.query_map("SubtensorModule", "Weights", [netuid]):
            uid = int(str(uid_key))
            raw = weights_val.value if hasattr(weights_val, "value") else weights_val
            if raw:
                weights_map[uid] = [[int(w[0]), round(int(w[1]) / U16_MAX, 6)] for w in raw]
    except Exception as e:
        return {"error": str(e)}
    return {"netuid": netuid, "weights": weights_map}


def get_subnet_identity(sub, netuid: int) -> dict:
    try:
        result = sub.substrate.query("SubtensorModule", "SubnetIdentitiesV3", [netuid])
        if result is None:
            return {}
        val = result.value if hasattr(result, "value") else result
        if not val:
            return {}
        identity = {}
        if isinstance(val, dict):
            for field in ("name", "url", "github_repo", "repository", "github", "description", "discord", "twitter", "image"):
                v = val.get(field)
                if v:
                    identity[field] = str(v)
        return identity
    except Exception as e:
        return {"error": str(e)}


def get_network_info(sub) -> dict:
    result = {}
    try:
        result["block"] = sub.get_current_block()
    except Exception:
        pass
    try:
        ti = sub.total_issuance()
        result["total_issuance_tao"] = float(ti.tao) if hasattr(ti, "tao") else float(ti)
    except Exception:
        pass
    try:
        ts = sub.total_stake()
        result["total_stake_tao"] = float(ts.tao) if hasattr(ts, "tao") else float(ts)
    except Exception:
        pass
    return result


def get_hotkey_info(sub, hotkey: str) -> dict:
    result = {"hotkey": hotkey}
    try:
        stake = sub.get_total_stake_for_hotkey(hotkey)
        result["total_stake_tao"] = float(stake.tao) if hasattr(stake, "tao") else float(stake)
    except Exception as e:
        result["stake_error"] = str(e)
    try:
        netuids = sub.get_netuids_for_hotkey(hotkey)
        result["subnets"] = list(netuids)
    except Exception as e:
        result["subnets_error"] = str(e)
    return result


def make_subtensor():
    import bittensor as bt
    cls = getattr(bt, "Subtensor", None) or getattr(bt, "subtensor", None)
    return cls(network="finney")


def handle(sub, cmd):
    action = cmd.get("action")
    if action == "subnet_detail":
        return get_subnet_detail(sub, cmd["netuid"])
    elif action == "metagraph":
        return get_metagraph_summary(sub, cmd["netuid"])
    elif action == "list_storage":
        return list_subtensor_storage(sub)
    elif action == "all_subnets":
        return get_all_subnets(sub)
    elif action == "subnet_identity":
        return get_subnet_identity(sub, cmd["netuid"])
    elif action == "weights":
        return get_weights(sub, cmd["netuid"])
    elif action == "network_info":
        return get_network_info(sub)
    elif action == "hotkey_info":
        return get_hotkey_info(sub, cmd["hotkey"])
    else:
        return {"error": f"unknown action: {action}"}


def main():
    import io
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    sub = make_subtensor()
    print(json.dumps({"status": "ready"}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
            result = handle(sub, cmd)
            # If the call failed, reconnect and retry once
            if "error" in result:
                try:
                    sub = make_subtensor()
                    result = handle(sub, cmd)
                except Exception as e:
                    result = {"error": f"reconnect failed: {e}"}
        except Exception as e:
            result = {"error": str(e)}

        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
