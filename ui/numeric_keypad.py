import tkinter as tk
from tkinter import messagebox
from typing import Callable


_BG_ROOT = "#05070b"
_BG_PANEL = "#0d1117"
_BG_KEY = "#161b22"
_BG_KEY_ACTIVE = "#202733"
_BG_CANCEL = "#11161d"
_BG_CANCEL_ACTIVE = "#1a222d"
_BG_CLEAR = "#301316"
_BG_CLEAR_ACTIVE = "#43191d"
_BG_SAVE = "#123019"
_BG_SAVE_ACTIVE = "#184222"
_FG_PRIMARY = "#f3f4f6"
_FG_SECONDARY = "#c6ccd4"
_FG_MUTED = "#9ba3ae"
_BORDER_PANEL = "#303844"
_BORDER_KEY = "#3a4452"
_BORDER_GREEN = "#84cc16"
_BORDER_RED = "#b91c1c"


def open_numeric_keypad_dialog(
    owner,
    *,
    title: str,
    range_text: str,
    initial_value: str,
    on_save: Callable[[str], None],
    error_title: str = "Error",
    error_mode: str = "error",
) -> None:
    parent_window = owner.winfo_toplevel()
    parent_window.update_idletasks()
    screen_width = max(360, int(parent_window.winfo_screenwidth()))
    screen_height = max(300, int(parent_window.winfo_screenheight()))
    base_scale = float(getattr(owner, "_ui_scale", 1.0))
    if screen_height <= 480:
        layout_scale = min(base_scale, 0.74)
    elif screen_height <= 520:
        layout_scale = min(base_scale, 0.82)
    elif screen_height <= 640:
        layout_scale = min(base_scale, 0.90)
    else:
        layout_scale = base_scale

    def sp(value: float, minimum: int = 0) -> int:
        return max(int(minimum), int(round(float(value) * layout_scale)))

    dialog = tk.Toplevel(owner)
    dialog.title(f"Editar: {title}")
    dialog.configure(bg=_BG_ROOT)
    dialog.resizable(False, False)
    try:
        dialog.attributes("-topmost", True)
    except tk.TclError:
        pass
    dialog.transient(parent_window)

    width = min(max(sp(460, 340), 340), max(340, screen_width - 20))
    height = min(max(sp(520, 400), 400), max(400, screen_height - 20))
    center_x = parent_window.winfo_x() + parent_window.winfo_width() // 2
    center_y = parent_window.winfo_y() + parent_window.winfo_height() // 2
    pos_x = max(0, center_x - width // 2)
    pos_y = max(0, center_y - height // 2)
    dialog.geometry(f"{width}x{height}+{pos_x}+{pos_y}")

    dialog.focus_force()
    dialog.grab_set()

    shell = tk.Frame(dialog, bg=_BG_ROOT, padx=sp(12, 8), pady=sp(10, 8))
    shell.pack(fill="both", expand=True)

    tk.Label(
        shell,
        text=title,
        font=("Arial", sp(18, 14), "bold"),
        bg=_BG_ROOT,
        fg=_FG_PRIMARY,
    ).pack(pady=(sp(6, 3), sp(2, 1)))
    tk.Label(
        shell,
        text=range_text,
        font=("Arial", sp(11, 9)),
        bg=_BG_ROOT,
        fg=_FG_SECONDARY,
    ).pack(pady=(0, sp(8, 5)))

    var_edit = tk.StringVar(value=str(initial_value))
    replace_on_first_input = True

    value_entry = tk.Entry(
        shell,
        textvariable=var_edit,
        justify="center",
        relief="flat",
        bd=0,
        bg=_BG_PANEL,
        fg=_FG_PRIMARY,
        insertbackground=_FG_PRIMARY,
        highlightthickness=2,
        highlightbackground=_BORDER_GREEN,
        highlightcolor=_BORDER_GREEN,
        font=("Arial", sp(23, 18), "bold"),
    )
    value_entry.pack(fill="x", ipady=sp(11, 6), pady=(0, sp(8, 5)))
    value_entry.icursor("end")
    value_entry.focus_set()

    def focus_entry() -> None:
        value_entry.focus_set()
        value_entry.icursor("end")

    def add_digit(digit: str) -> None:
        nonlocal replace_on_first_input
        if replace_on_first_input:
            var_edit.set(digit)
            replace_on_first_input = False
        else:
            var_edit.set(f"{var_edit.get()}{digit}")
        focus_entry()

    def add_decimal() -> None:
        nonlocal replace_on_first_input
        current = var_edit.get()
        if replace_on_first_input:
            var_edit.set("0.")
            replace_on_first_input = False
        elif "." not in current:
            var_edit.set(f"{current}.")
        focus_entry()

    def delete_last() -> None:
        nonlocal replace_on_first_input
        current = var_edit.get()
        if replace_on_first_input:
            var_edit.set("")
        else:
            var_edit.set(current[:-1] if current else "")
        focus_entry()

    def clear_all() -> None:
        nonlocal replace_on_first_input
        var_edit.set("")
        replace_on_first_input = False
        focus_entry()

    def on_keypress(event) -> str | None:
        nonlocal replace_on_first_input
        if event.keysym == "Return":
            save_and_close()
            return "break"
        if event.keysym == "Escape":
            on_cancel()
            return "break"
        if event.keysym == "BackSpace":
            replace_on_first_input = False
            return None

        char = event.char or ""
        if char.isdigit():
            if replace_on_first_input:
                var_edit.set(char)
                replace_on_first_input = False
                return "break"
            return None
        if char in (".", ","):
            add_decimal()
            return "break"
        return None

    value_entry.bind("<KeyPress>", on_keypress)

    tk.Label(
        shell,
        text="Teclado",
        font=("Arial", sp(12, 10)),
        bg=_BG_ROOT,
        fg=_FG_SECONDARY,
        anchor="w",
    ).pack(fill="x", pady=(0, sp(4, 3)))

    keypad_box = tk.Frame(
        shell,
        bg=_BG_PANEL,
        highlightthickness=1,
        highlightbackground=_BORDER_PANEL,
        bd=0,
    )
    keypad_box.pack(fill="both", expand=True)

    key_grid = tk.Frame(keypad_box, bg=_BG_PANEL, padx=sp(8, 6), pady=sp(8, 6))
    key_grid.pack(fill="both", expand=True)
    for row in range(4):
        key_grid.grid_rowconfigure(row, weight=1, uniform="keypad_rows")
    for column in range(3):
        key_grid.grid_columnconfigure(column, weight=1, uniform="keypad_cols")

    def make_button(
        parent,
        *,
        text: str,
        command,
        bg: str = _BG_KEY,
        active_bg: str = _BG_KEY_ACTIVE,
        fg: str = _FG_PRIMARY,
        border: str = _BORDER_KEY,
        font_size: int | None = None,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=fg,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
            font=("Arial", sp(font_size or 16, 12), "bold"),
            padx=sp(6, 4),
            pady=sp(6, 4),
            takefocus=0,
        )

    keypad_layout = (
        ("7", lambda: add_digit("7")),
        ("8", lambda: add_digit("8")),
        ("9", lambda: add_digit("9")),
        ("4", lambda: add_digit("4")),
        ("5", lambda: add_digit("5")),
        ("6", lambda: add_digit("6")),
        ("1", lambda: add_digit("1")),
        ("2", lambda: add_digit("2")),
        ("3", lambda: add_digit("3")),
        ("0", lambda: add_digit("0")),
        (".", add_decimal),
        ("<-", delete_last),
    )

    for index, (text, command) in enumerate(keypad_layout):
        row = index // 3
        column = index % 3
        make_button(key_grid, text=text, command=command).grid(
            row=row,
            column=column,
            sticky="nsew",
            padx=sp(4, 2),
            pady=sp(4, 2),
        )

    make_button(
        keypad_box,
        text="Borrar todo",
        command=clear_all,
        bg=_BG_CLEAR,
        active_bg=_BG_CLEAR_ACTIVE,
        fg="#f87171",
        border=_BORDER_RED,
        font_size=13,
    ).pack(fill="x", padx=sp(8, 6), pady=(0, sp(8, 6)))

    actions = tk.Frame(shell, bg=_BG_ROOT)
    actions.pack(fill="x", pady=(sp(8, 5), 0))
    actions.grid_columnconfigure(0, weight=1, uniform="actions")
    actions.grid_columnconfigure(1, weight=1, uniform="actions")

    def show_validation_error(message: str) -> None:
        if error_mode == "warning":
            messagebox.showwarning(error_title, message, parent=dialog)
        else:
            messagebox.showerror(error_title, message, parent=dialog)

    def save_and_close() -> None:
        try:
            on_save(var_edit.get().strip())
        except Exception as exc:
            show_validation_error(str(exc))
            return
        dialog.destroy()

    def on_cancel() -> None:
        dialog.destroy()

    make_button(
        actions,
        text="Guardar",
        command=save_and_close,
        bg=_BG_SAVE,
        active_bg=_BG_SAVE_ACTIVE,
        fg="#86efac",
        border="#4ade80",
        font_size=13,
    ).grid(row=0, column=0, sticky="ew", padx=(0, sp(6, 4)))
    make_button(
        actions,
        text="Cancelar",
        command=on_cancel,
        bg=_BG_CANCEL,
        active_bg=_BG_CANCEL_ACTIVE,
        fg=_FG_SECONDARY,
        border=_BORDER_KEY,
        font_size=13,
    ).grid(row=0, column=1, sticky="ew", padx=(sp(6, 4), 0))

    dialog.protocol("WM_DELETE_WINDOW", on_cancel)
    dialog.wait_window()
