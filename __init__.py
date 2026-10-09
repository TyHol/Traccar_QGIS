# Copyright (C) 2026 TyHol
# SPDX-License-Identifier: GPL-2.0-or-later

def classFactory(iface):
    from .traccar_live import TraccarLive
    return TraccarLive(iface)
