# -*- coding: utf-8 -*-
"""Main plugin entry point."""

from qgis.core import QgsApplication
from .provider import OpenGeoEnrichProvider


class OpenGeoEnrichPlugin:
    """Registers the OpenGeoEnrich Processing provider."""

    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initGui(self):
        self.provider = OpenGeoEnrichProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
