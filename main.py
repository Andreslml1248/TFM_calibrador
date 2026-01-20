#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
main.py
Punto de entrada de la aplicación (refactorizado)
- NO modifica: config.py, control_pi.py, mode_manual.py, mode_auto.py
- Usa:
    ✔ mode_manual.py (estable)
    ✔ mode_auto.py (corregido con punto 0 kPa correcto)
"""

from ui.app import App


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

