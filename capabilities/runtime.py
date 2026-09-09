from __future__ import annotations

import json
import re
from collections.abc import Mapping


class CapabilityError(ValueError):
    """An unsupported contract, boundary violation, or unavailable capability."""


MAX_BYTES = 128_000
MAX_ITEMS = 2_000
MAX_STEPS = 32
_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_CAPABILITY = re.compile(r"private\.[a-z][a-z0-9_.]{0,79}\Z")


def canonical(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise CapabilityError("value must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_BYTES:
        raise CapabilityError("value exceeds capability size limit")
    return encoded


def capability_identity(capability_id: str, version: int) -> None:
    if not isinstance(capability_id, str) or not _CAPABILITY.fullmatch(capability_id):
        raise CapabilityError("capability_id must use the private.<name> namespace")
    if type(version) is not int or not 1 <= version <= 1_000_000:
        raise CapabilityError("capability version must be a positive bounded integer")


def validate_schema(schema: object, *, depth: int = 0) -> None:
    """Deliberately small, closed subset; unsupported JSON Schema is rejected."""
    if depth > 8 or not isinstance(schema, dict):
        raise CapabilityError("schema must be a bounded object")
    kind = schema.get("type")
    if not isinstance(kind, str):
        raise CapabilityError("schema type must be text")
    if kind in {"string", "integer", "boolean"}:
        if set(schema) != {"type"}:
            raise CapabilityError("unsupported scalar schema fields")
    elif kind == "array":
        if set(schema) != {"type", "items"}:
            raise CapabilityError("array schema needs only type and items")
        validate_schema(schema["items"], depth=depth + 1)
    elif kind == "object":
        if set(schema) != {"type", "properties", "required", "additionalProperties"}:
            raise CapabilityError("object schema must declare properties, required and additionalProperties")
        if schema["additionalProperties"] is not False:
            raise CapabilityError("additional properties are not supported")
        props, required = schema["properties"], schema["required"]
        if not isinstance(props, dict) or len(props) > 32 or not all(
            isinstance(key, str) and _NAME.fullmatch(key) for key in props
        ):
            raise CapabilityError("schema property names or count are invalid")
        if not isinstance(required, list) or not all(isinstance(key, str) for key in required):
            raise CapabilityError("required must be a list of property names")
        if len(set(required)) != len(required) or set(required) - set(props):
            raise CapabilityError("required properties must be unique and declared")
        for child in props.values():
            validate_schema(child, depth=depth + 1)
    else:
        raise CapabilityError("unsupported schema type")


def validate_value(value: object, schema: dict, *, depth: int = 0) -> None:
    if depth > 8:
        raise CapabilityError("value is nested too deeply")
    kind = schema["type"]
    if kind == "string":
        valid = isinstance(value, str)
    elif kind == "integer":
        valid = type(value) is int and -(2**63) <= value < 2**63
    elif kind == "boolean":
        valid = type(value) is bool
    elif kind == "array":
        valid = isinstance(value, list) and len(value) <= MAX_ITEMS
        if valid:
            for item in value:
                validate_value(item, schema["items"], depth=depth + 1)
    elif kind == "object":
        valid = isinstance(value, dict)
        if valid:
            valid = not (set(value) - set(schema["properties"])) and not (
                set(schema["required"]) - set(value)
            )
        if valid:
            for key, item in value.items():
                validate_value(item, schema["properties"][key], depth=depth + 1)
    else:
        valid = False
    if not valid:
        raise CapabilityError(f"value does not match {kind} contract")
    canonical(value)


SERVICE_CONTRACTS = {
    "text.lines": {"text": "string"},
    "text.lower": {"text": "string"},
    "text.upper": {"text": "string"},
    "text.trim": {"text": "string"},
    "lines.containing": {"lines": "lines", "text": "string"},
    "lines.starting": {"lines": "lines", "text": "string"},
    "lines.trim": {"lines": "lines"},
    "lines.strip_prefix": {"lines": "lines", "prefix": "string"},
    "lines.nonempty": {"lines": "lines"},
    "lines.unique": {"lines": "lines"},
    "lines.sorted": {"lines": "lines"},
    "lines.join": {"lines": "lines", "separator": "string"},
    "lines.count": {"lines": "lines"},
}


def _service(name: str, args: dict) -> object:
    contracts = SERVICE_CONTRACTS.get(name)
    if contracts is None or set(args) != set(contracts):
        raise CapabilityError("unsupported service or argument names")
    for key, kind in contracts.items():
        value = args[key]
        if kind == "string":
            valid = isinstance(value, str)
        else:
            valid = isinstance(value, list) and len(value) <= MAX_ITEMS and all(
                isinstance(item, str) for item in value
            )
        if not valid:
            raise CapabilityError(f"invalid argument {key} for {name}")
    if name == "text.lines":
        result = args["text"].splitlines()
    elif name == "text.lower":
        result = args["text"].lower()
    elif name == "text.upper":
        result = args["text"].upper()
    elif name == "text.trim":
        result = args["text"].strip()
    elif name == "lines.containing":
        result = [item for item in args["lines"] if args["text"] in item]
    elif name == "lines.starting":
        result = [item for item in args["lines"] if item.startswith(args["text"])]
    elif name == "lines.trim":
        result = [item.strip() for item in args["lines"]]
    elif name == "lines.strip_prefix":
        result = [item.removeprefix(args["prefix"]) for item in args["lines"]]
    elif name == "lines.nonempty":
        result = [item for item in args["lines"] if item]
    elif name == "lines.unique":
        result = list(dict.fromkeys(args["lines"]))
    elif name == "lines.sorted":
        result = sorted(args["lines"])
    elif name == "lines.join":
        estimated = sum(len(item.encode("utf-8")) for item in args["lines"]) + (
            max(0, len(args["lines"]) - 1) * len(args["separator"].encode("utf-8"))
        )
        if estimated > MAX_BYTES:
            raise CapabilityError("joined output exceeds capability size limit")
        result = args["separator"].join(args["lines"])
    else:
        result = len(args["lines"])
    if isinstance(result, list) and len(result) > MAX_ITEMS:
        raise CapabilityError("service output exceeds item limit")
    canonical(result)
    return result


def _resolve(expr: object, values: Mapping[str, object], *, depth: int = 0) -> object:
    if depth > 8:
        raise CapabilityError("expression nesting exceeds limit")
    if isinstance(expr, dict):
        if set(expr) == {"ref"}:
            ref = expr["ref"]
            if not isinstance(ref, str) or len(ref) > 240:
                raise CapabilityError("invalid reference")
            parts = ref.split(".")
            current: object = values
            for part in parts:
                if not isinstance(current, Mapping) or part not in current:
                    raise CapabilityError(f"unresolved reference: {ref}")
                current = current[part]
            return current
        result = {}
        size = 2
        for key, item in expr.items():
            resolved = _resolve(item, values, depth=depth + 1)
            size += len(canonical(key).encode("utf-8")) + len(canonical(resolved).encode("utf-8")) + 2
            if size > MAX_BYTES:
                raise CapabilityError("resolved object exceeds capability size limit")
            result[key] = resolved
        return result
    if isinstance(expr, list):
        if len(expr) > MAX_ITEMS:
            raise CapabilityError("expression exceeds item limit")
        result = []
        size = 2
        for item in expr:
            resolved = _resolve(item, values, depth=depth + 1)
            size += len(canonical(resolved).encode("utf-8")) + 1
            if size > MAX_BYTES:
                raise CapabilityError("resolved list exceeds capability size limit")
            result.append(resolved)
        return result
    return expr


class RecipeRuntime:
    """A closed interpreter over Hikari-owned pure services; never eval/import/exec."""

    @staticmethod
    def validate(recipe: object) -> dict:
        canonical(recipe)
        if not isinstance(recipe, dict) or set(recipe) != {
            "format", "capability_id", "version", "owner", "permissions",
            "input_schema", "output_schema", "steps", "return",
        }:
            raise CapabilityError("invalid recipe fields")
        if recipe["format"] != "hikari.recipe.v1" or recipe["owner"] != "hikari.private":
            raise CapabilityError("unsupported recipe format or owner")
        capability_identity(recipe["capability_id"], recipe["version"])
        if recipe["permissions"] != []:
            raise CapabilityError("recipe permission expansion is unsupported")
        validate_schema(recipe["input_schema"])
        validate_schema(recipe["output_schema"])
        steps = recipe["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise CapabilityError("recipe must contain 1..32 service calls")
        names = {"input"}
        for step in steps:
            if not isinstance(step, dict) or set(step) != {"id", "service", "args"}:
                raise CapabilityError("invalid recipe step")
            name = step["id"]
            if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
                raise CapabilityError("recipe step ids must be valid and unique")
            names.add(name)
            service = step["service"]
            if not isinstance(service, str) or service not in SERVICE_CONTRACTS:
                raise CapabilityError("unsupported service; request a native implementation")
            if not isinstance(step["args"], dict) or set(step["args"]) != set(SERVICE_CONTRACTS[service]):
                raise CapabilityError("invalid service arguments")
        return recipe

    def invoke(self, recipe: dict, inputs: object) -> object:
        self.validate(recipe)
        validate_value(inputs, recipe["input_schema"])
        values = {"input": inputs}
        for step in recipe["steps"]:
            args = _resolve(step["args"], values)
            values[step["id"]] = _service(step["service"], args)
        result = _resolve(recipe["return"], values)
        validate_value(result, recipe["output_schema"])
        return json.loads(canonical(result))
