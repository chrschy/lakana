"""Helpers that tests of more than one package share.

This is test code only: nothing here is installed with a package or imported by one. It lives
under ``scripts`` because that directory is already on the import path of every test run
(``pythonpath = ["."]``), so a test imports it by its full name and needs no ``sys.path`` edit.
"""
