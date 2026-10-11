"""Simulator tests never upload to Databricks, even on a laptop whose backend/.env has the Databricks login."""
import os

os.environ['DATABRICKS_UPLOAD'] = 'off'
