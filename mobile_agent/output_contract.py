"""Result presentation contracts, separate from canonical extraction data."""

from .extraction import validate_schema


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
