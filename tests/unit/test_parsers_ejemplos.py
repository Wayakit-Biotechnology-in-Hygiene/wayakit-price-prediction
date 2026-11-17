"""Ejemplos concretos para los parsers de ``scraper/utils.py``.

Complementan las propiedades de ``tests/property/test_propiedades_parsers.py`` con títulos
parecidos a los reales y con los contraejemplos mínimos de los bugs encontrados (cada uno como
``xfail(strict=True)``: cuando pi-dev arregle el bug, la prueba pasa, el xfail estricto la marca
como falla y hay que quitar la marca).
"""
import pytest

from tests.conftest import cargar_modulo

utils = cargar_modulo("scraper/utils.py")

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "titulo, esperado",
    [
        ("Dettol Antibacterial Surface Cleaner 500ml", {"quantity": 500.0, "unit": "ml", "normalized": 500.0}),
        ("Fairy Dishwashing Liquid 1.5 L", {"quantity": 1.5, "unit": "L", "normalized": 1500.0}),
        ("Clorox Bleach 3.78 Liters", {"quantity": 3.78, "unit": "L", "normalized": 3780.0}),
        ("Glass cleaner 32 fl oz", {"quantity": 32.0, "unit": "fl oz", "normalized": 32 * 29.5735}),
        ("Car wax 16 FL. OZ", None),  # "FL." con punto no está soportado; queda documentado
        ("Detergent powder 2 kg", {"quantity": 2.0, "unit": "kg", "normalized": 2000.0}),
        ("Detergent powder 500 g", {"quantity": 500.0, "unit": "g", "normalized": 500.0}),
        ("Microfiber cloth", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_volume_string_titulos(titulo, esperado):
    resultado = utils.parse_volume_string(titulo)
    if esperado is None:
        assert resultado is None
    else:
        assert resultado["quantity"] == pytest.approx(esperado["quantity"])
        assert resultado["unit"] == esperado["unit"]
        assert resultado["normalized"] == pytest.approx(esperado["normalized"])


@pytest.mark.xfail(
    strict=True,
    reason="BUG: 'Shampoo 1,000 ml' devuelve quantity=0.0: la regex no acepta separador de miles "
    "y toma solo '000' (hallazgo para pi-dev)",
)
def test_volumen_con_separador_de_miles():
    assert utils.parse_volume_string("Shampoo 1,000 ml")["quantity"] == 1000.0


@pytest.mark.xfail(
    strict=True,
    reason="BUG: '.5 L' devuelve quantity=5.0 (5 L, no 0.5 L): la regex exige un dígito antes del "
    "punto decimal (hallazgo para pi-dev)",
)
def test_volumen_decimal_sin_cero_inicial():
    assert utils.parse_volume_string("Bleach .5 L")["quantity"] == 0.5


@pytest.mark.parametrize(
    "titulo, cantidad",
    [
        ("Sanitizing Wipes 80 wipes", 80),
        ("Dettol 40 Wet Wipes", 40),
        ("Paper towels pack of 6", 6),
        ("Gloves Box of 100", 100),
        ("Sachets 12/box", 12),
        ("Wipes 72 pcs", 72),
        ("Microfiber cloth", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_count_string_titulos(titulo, cantidad):
    resultado = utils.parse_count_string(titulo)
    if cantidad is None:
        assert resultado is None
    else:
        assert resultado == {"quantity": cantidad, "unit": "units", "normalized": cantidad}


@pytest.mark.parametrize(
    "titulo, cantidad",
    [("Microfiber 12-Piece Set", 12), ("Cleaning rags 50 - rags", 50), ("Wipes 80-wipes", 80), ("Sponge", None), ("", None)],
)
def test_parse_saco_count_string_titulos(titulo, cantidad):
    resultado = utils.parse_saco_count_string(titulo)
    assert (resultado or {}).get("quantity") == cantidad


@pytest.mark.parametrize(
    "titulo, cantidad, unidad",
    [
        ("Hand soap (6 Bottles x 500 ml)", 3000.0, "ml"),
        ("Floor cleaner 4 Pcs x 5 Ltr", 20.0, "L"),
        ("Dish soap 12 x 750 ml", 9000.0, "ml"),
        ("Dish soap 750 ml", 750.0, "ml"),
    ],
)
def test_parse_volume_with_multiplier_titulos(titulo, cantidad, unidad):
    resultado = utils.parse_volume_with_multiplier(titulo)
    assert resultado["quantity"] == pytest.approx(cantidad)
    assert resultado["unit"] == unidad


@pytest.mark.xfail(
    strict=True,
    reason="BUG: '(2 Bottles x 0.00001 ml)' devuelve quantity=2e-05 pero normalized=5.0: el total "
    "se vuelve a parsear con f'{total}', que escribe '2e-05' y la regex lee '05'. Pasa con totales "
    "< 1e-4 o >= 1e16 (poco realista; hallazgo menor para pi-dev)",
)
def test_multiplicador_notacion_cientifica_mantiene_normalizacion():
    resultado = utils.parse_volume_with_multiplier("(2 Bottles x 0.00001 ml)")
    assert resultado["normalized"] == pytest.approx(resultado["quantity"])


@pytest.mark.xfail(
    strict=True,
    reason="Posible BUG (confirmar con Mafer): 'Detergent 3X concentrate 500 ml' devuelve 1500 ml; "
    "el '3X' de concentración se toma como 3 piezas y el precio por litro baja a un tercio "
    "(hallazgo para pi-dev)",
)
def test_multiplicador_no_confunde_concentracion_con_piezas():
    assert utils.parse_volume_with_multiplier("Detergent 3X concentrate 500 ml")["quantity"] == 500.0


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="BUG menor: un número de más de 4300 dígitos ('9'*4301 + ' wipes') lanza ValueError en "
    "int() por el límite de Python 3.10.7+; los parsers de conteo no lo atrapan. Irreal en títulos, "
    "pero rompe la regla 'nunca lanza' (hallazgo para pi-dev)",
)
def test_conteo_numero_gigante_no_lanza():
    utils.parse_count_string("9" * 4301 + " wipes")


def test_aerosense_ejemplos():
    assert utils.extract_aerosense_units("AeroSense refill (6 pcs)") == 6
    assert utils.extract_aerosense_units("AeroSense 3-pack x 2") == 6
    assert utils.extract_aerosense_units("AeroSense refill") is None
