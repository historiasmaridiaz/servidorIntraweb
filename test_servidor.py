#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script de pruebas unitarias e integración para el servidor local de Historias Clínicas.
"""

from __future__ import annotations

import json
import time
import unittest
from urllib.request import urlopen, Request

import intranet_motor as INTRANET_MOTOR


class TestIntranetMotor(unittest.TestCase):

    def test_document_parsing(self):
        doc, tipo = INTRANET_MOTOR.parse_document_input("CC 1192899684")
        self.assertEqual(doc, "1192899684")
        self.assertEqual(tipo, "CC")

        doc2, tipo2 = INTRANET_MOTOR.parse_document_input("12345678", "TI")
        self.assertEqual(doc2, "12345678")
        self.assertEqual(tipo2, "TI")

    def test_table_extraction(self):
        sample_html = """
        <html>
        <body>
            <table class="datos">
                <tr><th>Tipo Documento</th><th>No Documento</th><th>Nombre y Apellidos</th><th>Entidad</th><th>Estado</th><th>Fecha Atencion</th></tr>
                <tr><td>CC</td><td>10852147</td><td>MARIA DE CARMEN LOPEZ</td><td>PROINSALUD EPS</td><td>ACTIVO</td><td>15/06/2026</td></tr>
            </table>
        </body>
        </html>
        """
        rows = INTRANET_MOTOR.extract_table_rows(sample_html)
        self.assertGreaterEqual(len(rows), 2)
        
        extracted = INTRANET_MOTOR.extract_from_header_rows(rows, "10852147")
        patient = INTRANET_MOTOR.normalize_patient_data(extracted, "10852147", "http://test.local")

        self.assertEqual(patient["cedula"], "10852147")
        self.assertEqual(patient["tipo_doc"], "CC")
        self.assertEqual(patient["nombre"], "MARIA DE CARMEN LOPEZ")
        self.assertEqual(patient["contrato"], "PROINSALUD EPS")
        self.assertEqual(patient["estado"], "ACTIVO")
        self.assertEqual(patient["ult_consulta"], "2026-06-15")


if __name__ == "__main__":
    unittest.main()
