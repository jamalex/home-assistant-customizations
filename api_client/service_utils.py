def flatten_fields(fields: dict) -> dict:
    """
    Some definitions place nested fields under 'advanced_fields'.
    This flattens them into a single dict for easy iteration.
    """
    result = {}
    for key, val in fields.items():
        # If this is the advanced-fields container, merge them in
        if key == "advanced_fields" and "fields" in val:
            for adv_key, adv_val in val["fields"].items():
                result[adv_key] = adv_val
        else:
            result[key] = val
    return result


def get_allowed_values(selector: dict) -> str:
    """
    Returns a short string summarizing allowed or possible values/ranges,
    parsed from the field's 'selector' definition.
    """
    if "number" in selector:
        num = selector["number"]
        min_ = num.get("min")
        max_ = num.get("max")
        unit = num.get("unit_of_measurement")
        if min_ is not None and max_ is not None:
            if unit:
                return f"(Range: {min_} to {max_} {unit})"
            else:
                return f"(Range: {min_} to {max_})"
        # If we have only a min or only a max, or none, just say numeric
        return "(numeric value)"
    elif "select" in selector:
        sel = selector["select"]
        options = sel.get("options", [])
        # Possibly: options can be just a list of strings or a list of dicts
        if options and isinstance(options[0], dict):
            # e.g. [{"label": "Long", "value": "long"}, ...]
            vals = [o.get("value") for o in options if "value" in o]
            return f"(Allowed values: {vals})"
        else:
            # e.g. ["homeassistant", "aliceblue", ...]
            return f"(Allowed values: {options})"
    elif "color_temp" in selector:
        # e.g. {"unit": "kelvin", "min": 2000, "max": 6500}
        ct = selector["color_temp"]
        unit = ct.get("unit", "kelvin")
        min_ = ct.get("min")
        max_ = ct.get("max")
        if min_ is not None and max_ is not None:
            return f"(Range: {min_} to {max_} {unit})"
        return f"(color_temp in {unit})"
    elif "color_rgb" in selector:
        return "(RGB color list, e.g. [255, 100, 100])"
    elif "object" in selector:
        return "(arbitrary object/dict)"
    elif "constant" in selector:
        val = selector["constant"].get("value")
        lab = selector["constant"].get("label")
        return f"(constant: {val}, label: {lab})"
    elif "text" in selector:
        # Possibly freeform text
        return "(text/string)"
    elif "time" in selector:
        return "(time string, e.g. HH:MM:SS)"
    elif "date" in selector:
        return "(date string, e.g. YYYY-MM-DD)"
    elif "datetime" in selector:
        return "(date+time string, e.g. 2023-11-17 13:30:00)"
    elif "boolean" in selector:
        return "(boolean: true/false)"
    elif "template" in selector:
        return "(template string or Jinja2 expression)"
    # ... handle more as needed ...
    return ""


def generate_function_code(domain: str, service_name: str, service_def: dict) -> str:
    """
    Creates a Python function definition for a given service definition.

    Example signature:  def light__turn_on(...):
        \"\"\"Description, then doc lines for each field...\"\"\"
    """
    func_lines = []
    indent = " " * 4

    # Top-level description
    description = service_def.get("description", "")
    # Extract fields, flatten advanced if present
    fields = flatten_fields(service_def.get("fields", {}))
    # Build param list, defaulting all to None (or maybe optional)
    # We skip "target" because it's not an actual field, usually a separate concept
    # param_names = list(fields.keys())
    # params_str = ", ".join(f"{p}=None" for p in param_names)
    params_str = ""

    func_lines.append(f"def wrapper_fn(entity):")

    func_lines.append(f"{indent}def {service_name}(**kwargs):")

    # Build docstring lines
    docstring_lines = []
    if description:
        docstring_lines.append(description.strip())

    # For each field, build doc line
    for field_key, field_info in fields.items():
        # Get the textual description
        field_desc = field_info.get("description", "").strip()
        # Example or required or both?
        example = field_info.get("example")
        required = field_info.get("required", False)

        # Try to glean allowed values from the selector
        selector = field_info.get("selector", {})
        allowed_str = get_allowed_values(selector)

        line = f"{field_key}: "
        if field_desc:
            line += field_desc
        if allowed_str:
            line += f"  {allowed_str}"
        if example is not None:
            line += f"  Example: {example}"
        if required:
            line += "  (required)"
        docstring_lines.append(line)

    if not docstring_lines:
        docstring = '"""No description."""'
    else:
        # Indent the docstring content
        docstring = '"""' + "\n\n{indent}{indent}".join(docstring_lines) + '\n"""'

    func_lines.append(f"{indent}{indent}{docstring}")

    # Add placeholder body
    func_lines.append(f"{indent}{indent}return entity.call_service('{service_name}', **kwargs)")

    # Return the inner function
    func_lines.append(f"{indent}return {service_name}")

    return "\n".join(func_lines)


def main():
    for domain_name, service_dict in ALL_SERVICES.items():
        # Each domain has multiple services
        for service_name, service_def in service_dict.items():
            code_str = generate_function_code(domain_name, service_name, service_def)
            print(code_str)
