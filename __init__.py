# -*- coding: utf-8 -*-
"""OpenGeoEnrich QGIS plugin."""


def classFactory(iface):
    from .plugin import OpenGeoEnrichPlugin
    return OpenGeoEnrichPlugin(iface)
