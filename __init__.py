def classFactory(iface):
    from .traccar_live import TraccarLive
    return TraccarLive(iface)
