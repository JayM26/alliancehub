from django import template  # pyright: ignore[reportMissingModuleSource]

register = template.Library()


@register.filter
def get_item(d, key):
    if d is None or key is None:
        return None
    try:
        return d.get(int(key)) or d.get(key)
    except Exception:
        return d.get(key)
