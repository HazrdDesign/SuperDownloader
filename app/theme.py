"""Super Downloader colors (dgnl.co branding) and the CustomTkinter theme built from them.

Every color in the app comes from this file. To rebrand, change the BRAND values below;
the status colors (green / amber / red) stay separate so success and failure are always
easy to tell apart.

dgnl.co palette: background #232323, white text, accent #FF3B33. Dark theme, single accent.
"""

from __future__ import annotations

import customtkinter as ctk

# --- Brand (dgnl.co) ------------------------------------------------------------------------------
BACKGROUND = "#232323"    # window background
TEXT = "#F2F2F2"          # main text (a soft white: easier on the eyes than pure white on dark)
ACCENT = "#FF3B33"        # buttons, progress bars, selected items
ACCENT_TEXT = "#FFFFFF"   # text on accent-colored buttons (pure white for the most contrast on the red)

# --- Derived neutrals (computed from the brand so the whole UI follows it) ----------------------


def _mix(a: str, b: str, t: float) -> str:
    """Blend hex color ``a`` towards ``b`` by ``t`` (0..1)."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


SURFACE = _mix(BACKGROUND, TEXT, 0.06)       # cards, inputs
SURFACE_2 = _mix(BACKGROUND, TEXT, 0.11)     # hovered rows, secondary buttons
BORDER = _mix(BACKGROUND, TEXT, 0.22)
MUTED = _mix(TEXT, BACKGROUND, 0.42)         # secondary text
ACCENT_HOVER = _mix(ACCENT, BACKGROUND, 0.18)
ACCENT_DIM = _mix(ACCENT, BACKGROUND, 0.55)

# Red is kept for the one main action on each screen (Download, Retry, Save...). Everything
# else uses this outlined style, so the accent stays meaningful.
SECONDARY_BUTTON = {"fg_color": "transparent", "border_width": 1, "border_color": BORDER,
                    "text_color": TEXT, "hover_color": SURFACE_2}

# --- Status (not brand) --------------------------------------------------------------------------
# Only problems are colored: amber for "you can fix this", red for "failed". Success stays neutral
# so the screen isn't full of colors.
WARN = "#E5B93A"
ERROR = "#E5534B"
WARN_BG = _mix(BACKGROUND, WARN, 0.16)
ERROR_BG = _mix(BACKGROUND, ERROR, 0.16)


def apply() -> None:
    """Load the dark theme and recolor every widget type with the brand palette.

    Must run before any widget is created.
    """
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    t = ctk.ThemeManager.theme

    def put(widget: str, **colors: str) -> None:
        for key, value in colors.items():
            t[widget][key] = [value, value]  # [light, dark]; the app is dark-only

    put("CTk", fg_color=BACKGROUND)
    put("CTkToplevel", fg_color=BACKGROUND)
    put("CTkFrame", fg_color=SURFACE, top_fg_color=SURFACE, border_color=BORDER)
    put("CTkButton", fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER,
        text_color=ACCENT_TEXT, text_color_disabled=MUTED)
    put("CTkLabel", text_color=TEXT)
    put("CTkEntry", fg_color=SURFACE, border_color=BORDER, text_color=TEXT, placeholder_text_color=MUTED)
    put("CTkCheckBox", fg_color=ACCENT, border_color=BORDER, hover_color=ACCENT_HOVER,
        checkmark_color=ACCENT_TEXT, text_color=TEXT, text_color_disabled=MUTED)
    put("CTkSwitch", fg_color=SURFACE_2, progress_color=ACCENT, button_color=TEXT, button_hover_color=TEXT,
        text_color=TEXT, text_color_disabled=MUTED)
    put("CTkRadioButton", fg_color=ACCENT, border_color=BORDER, hover_color=ACCENT_HOVER,
        text_color=TEXT, text_color_disabled=MUTED)
    put("CTkProgressBar", fg_color=SURFACE_2, progress_color=ACCENT, border_color=BORDER)
    put("CTkSlider", fg_color=SURFACE_2, progress_color=ACCENT_DIM, button_color=ACCENT, button_hover_color=ACCENT_HOVER)
    put("CTkOptionMenu", fg_color=SURFACE_2, button_color=SURFACE_2, button_hover_color=BORDER,
        text_color=TEXT, text_color_disabled=MUTED)
    put("CTkComboBox", fg_color=SURFACE, border_color=BORDER, button_color=BORDER, button_hover_color=MUTED,
        text_color=TEXT, text_color_disabled=MUTED)
    put("CTkScrollbar", button_color=BORDER, button_hover_color=MUTED)
    # Segmented buttons have one text color for every segment, so the selected segment uses a
    # toned-down accent that TEXT stays readable on.
    put("CTkSegmentedButton", fg_color=SURFACE_2, selected_color=ACCENT_DIM, selected_hover_color=ACCENT_DIM,
        unselected_color=SURFACE_2, unselected_hover_color=BORDER, text_color=TEXT, text_color_disabled=MUTED)
    put("CTkTextbox", fg_color=SURFACE, border_color=BORDER, text_color=TEXT,
        scrollbar_button_color=BORDER, scrollbar_button_hover_color=MUTED)
    put("CTkScrollableFrame", label_fg_color=SURFACE)
    put("DropdownMenu", fg_color=SURFACE, hover_color=SURFACE_2, text_color=TEXT)
