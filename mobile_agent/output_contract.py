"""Result presentation contracts, and the bounded JSON Schema subset a task's output may declare.

The schema subset is safe on purpose: it never fetches references, executes regular expressions or infers missing
values.
"""

import json

from .transport import decode_json

KEYWORDS = frozenset({"$schema", "type", "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "minimum",
    "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "enum", "const",
    "title", "description"})
TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


def bounded_json(value, *, max_bytes=32000, max_nodes=2000, max_depth=12):
    pending, count = [(value, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > max_nodes or depth > max_depth:
            raise ValueError("JSON exceeds structural limits")
        if type(node) is dict:
            if any(not isinstance(key, str) for key in node):
                raise ValueError("JSON object keys must be strings")
            pending.extend((child, depth + 1) for child in node.values())
        elif type(node) is list:
            pending.extend((child, depth + 1) for child in node)
        elif node is not None and type(node) not in (str, int, float, bool):
            raise ValueError("Result contains a non-JSON value")
    try:
        wire = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(wire.encode()) > max_bytes:
            raise ValueError("JSON exceeds byte limit")
        return decode_json(wire)
    except (TypeError, OverflowError, UnicodeError):
        raise ValueError("Invalid JSON value") from None


def validate_schema(schema):
    schema = bounded_json(schema, max_bytes=16000, max_nodes=500, max_depth=12)
    if not isinstance(schema, dict):
        raise ValueError("Output schema must be a JSON Schema object")
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        raise ValueError("Structured output requires mobile_agent/requirements.txt") from None

    def inspect(node, depth=0):
        if not isinstance(node, dict) or depth > 6 or set(node) - KEYWORDS:
            raise ValueError("Unsupported output schema keyword or nesting depth")
        declared = node.get("type")
        kinds = declared if isinstance(declared, list) else [declared]
        if not kinds or any(not isinstance(kind, str) or kind not in TYPES for kind in kinds):
            raise ValueError("Every output schema node requires an explicit supported type")
        if "$schema" in node and node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("Only JSON Schema Draft 2020-12 is supported")
        if "object" in kinds:
            properties = node.get("properties", {})
            if not isinstance(properties, dict) or len(properties) > 40 or node.get("additionalProperties") is not False:
                raise ValueError("Objects require bounded properties and additionalProperties:false")
            required = node.get("required", [])
            if not isinstance(required, list) or any(not isinstance(key, str) or key not in properties for key in required):
                raise ValueError("Required properties must have a defined schema")
            for child in properties.values():
                inspect(child, depth + 1)
        elif "properties" in node or "required" in node or "additionalProperties" in node:
            raise ValueError("Object keywords require object type")
        if "array" in kinds:
            if "items" not in node:
                raise ValueError("Arrays require a bounded item schema")
            inspect(node["items"], depth + 1)
            if type(node.get("maxItems")) is not int or not 0 <= node["maxItems"] <= 100:
                raise ValueError("Arrays require maxItems between 0 and 100")
        elif "items" in node:
            raise ValueError("Array items require array type")
        if "enum" in node and (not isinstance(node["enum"], list) or not 1 <= len(node["enum"]) <= 100):
            raise ValueError("Enum exceeds supported bounds")

    inspect(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise ValueError("Invalid JSON Schema") from None
    return schema



FORMATS = frozenset({"text", "json", "yaml", "csv", "markdown"})
SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean", "null"})


def validate_format(output_format):
    """An actual result format; Auto is a request policy, never a serializer."""
    if not isinstance(output_format, str) or output_format not in FORMATS:
        raise ValueError("Choose Text, JSON, YAML, CSV, or Markdown output")


def validate_output(output_format, schema):
    if output_format == "auto":
        if schema is not None:
            raise ValueError("Choose a structured format to supply a custom schema")
        return None
    validate_format(output_format)
    if output_format == "text" and schema is not None:
        raise ValueError("Choose a structured format to supply a custom schema")
    schema = validate_schema(schema) if schema is not None else None
    if output_format != "csv" or schema is None:
        return schema
    return _validate_csv_shape(schema)


def validate_automatic_schema(output_format, schema):
    """A model-selected schema is mandatory and bounded, including plain-text answers."""
    validate_format(output_format)
    schema = validate_schema(schema)
    if output_format == "csv":
        return _validate_csv_shape(schema)
    if output_format == "text" and schema["type"] != "string":
        # Automatic text is one string or one flat object; a table is CSV-only.
        if schema["type"] == "array":
            raise ValueError("Text requires a string or a flat object with named columns")
        return _validate_csv_shape(schema)
    return schema


def _validate_csv_shape(schema):
    row = schema.get("items") if schema.get("type") == "array" else schema
    if (not isinstance(row, dict) or row.get("type") != "object"
            or row.get("additionalProperties") is not False or not row.get("properties")):
        raise ValueError("CSV requires a flat object or an array of flat objects with named columns")
    for column in row["properties"].values():
        declared = column["type"]
        kinds = declared if isinstance(declared, list) else [declared]
        if any(kind not in SCALAR_TYPES for kind in kinds):
            raise ValueError("CSV columns must be scalar values, not nested objects or arrays")
    return schema
