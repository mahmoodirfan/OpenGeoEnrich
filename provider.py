# -*- coding: utf-8 -*-
"""Processing provider for OpenGeoEnrich."""

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
from .algorithm import OpenGeoEnrichAlgorithm
from .prepare_data_algorithm import OpenGeoEnrichPrepareDataAlgorithm
import os


class OpenGeoEnrichProvider(QgsProcessingProvider):
    """QGIS Processing provider."""

    def loadAlgorithms(self):
        self.addAlgorithm(OpenGeoEnrichPrepareDataAlgorithm())
        self.addAlgorithm(OpenGeoEnrichAlgorithm())

    def id(self):
        return 'opengeoenrich'

    def name(self):
        return 'OpenGeoEnrich'

    def longName(self):
        return 'OpenGeoEnrich'

    def icon(self):
        icon_path = os.path.join(os.path.dirname(__file__), 'icons', 'icon.png')
        return QIcon(icon_path)
