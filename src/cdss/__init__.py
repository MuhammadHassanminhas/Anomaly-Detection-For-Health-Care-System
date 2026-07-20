"""CDSS — anomaly detection over INDICI_BI_Full."""

from os import environ

from cdss._dotenv import load_dotenv

load_dotenv(environ)

__version__ = "0.1.0"
