"""Small Qt5/Qt6 compatibility helpers for the QGIS PyQt shim."""

from qgis.PyQt.QtCore import Qt as _OriginalQt


class _QtCompat:
    def __getattr__(self, name):
        mappings = {
            "AlignCenter": "AlignmentFlag",
            "AlignHCenter": "AlignmentFlag",
            "AlignLeft": "AlignmentFlag",
            "AlignRight": "AlignmentFlag",
            "AlignTop": "AlignmentFlag",
            "AlignVCenter": "AlignmentFlag",
            "ArrowCursor": "CursorShape",
            "CrossCursor": "CursorShape",
            "DashLine": "PenStyle",
            "ItemIsEditable": "ItemFlag",
            "KeepAspectRatio": "AspectRatioMode",
            "LeftArrow": "ArrowType",
            "LeftButton": "MouseButton",
            "RightArrow": "ArrowType",
            "RichText": "TextFormat",
            "ShiftModifier": "KeyboardModifier",
            "SmoothTransformation": "TransformationMode",
            "TextWordWrap": "TextFlag",
            "UserRole": "ItemDataRole",
            "WindowModal": "WindowModality",
            "RightButton": "MouseButton",
            "Checked": "CheckState",
            "Unchecked": "CheckState",
            "PartiallyChecked": "CheckState",
            "ScrollBarAlwaysOff": "ScrollBarPolicy",
            "ScrollBarAsNeeded": "ScrollBarPolicy",
            "WrapAnywhere": "WrapMode",
            "ElideRight": "TextElideMode",
            "Horizontal": "Orientation",
            "Vertical": "Orientation",
            "PointingHandCursor": "CursorShape",
        }
        if hasattr(_OriginalQt, name):
            return getattr(_OriginalQt, name)
        if name in mappings:
            group_name = mappings[name]
            if hasattr(_OriginalQt, group_name):
                group = getattr(_OriginalQt, group_name)
                if hasattr(group, name):
                    return getattr(group, name)
        return getattr(_OriginalQt, name)


QtCompat = _QtCompat()


def ensure_qt_compat(qt):
    # Backward compatibility for old code that calls ensure_qt_compat(Qt)
    return qt
