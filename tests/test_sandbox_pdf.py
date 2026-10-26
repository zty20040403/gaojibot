import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("reportlab") and importlib.util.find_spec("pypdf"),
    "Requires sandbox PDF dependencies")
class SandboxPdfTests(unittest.TestCase):
    def test_helper_embeds_initial_font_instead_of_leaving_helvetica(self):
        import reportlab
        from pypdf import PdfReader
        source = Path(__file__).resolve().parents[1] / "src/plugins/ai_chat/sandbox_pdf.py"
        spec = importlib.util.spec_from_file_location("sandbox_pdf_helper", source)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "source.md"
            output_path = Path(directory) / "output.pdf"
            input_path.write_text("# Acceptance title\n\nOne-page document.\n")
            font = str(Path(reportlab.__file__).parent / "fonts/Vera.ttf")
            with patch.object(helper, "REGULAR_FONT", font), patch("sys.argv",
                    ["gaoji-pdf", str(input_path), str(output_path)]):
                self.assertEqual(helper.main(), 0)
            reader = PdfReader(output_path)
            self.assertEqual(len(reader.pages), 1)
            self.assertIn("Acceptance title", reader.pages[0].extract_text())
            fonts = reader.pages[0]["/Resources"]["/Font"].values()
            self.assertTrue(fonts)
            for reference in fonts:
                font = reference.get_object()
                self.assertIn("/FontFile2", font["/FontDescriptor"])
