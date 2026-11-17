"""Propiedades de los parsers de tamaño y conteo de ``scraper/utils.py``.

Cada prueba genera cientos de entradas con Hypothesis y revisa reglas que deben cumplirse
siempre (no lanza, rangos, unidades permitidas, ida y vuelta del número, relaciones de
normalización). Si una regla falla por un bug real de producción, no se arregla el código aquí
ni se debilita la regla: la propiedad general excluye solo el contraejemplo conocido (estrategia
filtrada) y sigue corriendo, y el bug queda en una prueba mínima ``xfail(strict=True)`` que solo
corre su ``@example`` (``phases=[Phase.explicit]``) y nombra el bug en ``reason``.
"""
import math

import pytest
from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st

from tests.conftest import cargar_modulo

utils = cargar_modulo("scraper/utils.py")

pytestmark = pytest.mark.property

# Unidades canónicas que devuelve parse_volume_string y su factor de normalización.
FACTOR_POR_UNIDAD = {"ml": 1, "L": 1000, "g": 1, "kg": 1000, "fl oz": 29.5735}

# Cada forma escrita que acepta la expresión regular -> (unidad canónica esperada, factor).
FORMAS_DE_VOLUMEN = {
    "ml": ("ml", 1),
    "milliliters": ("ml", 1),
    "l": ("L", 1000),
    "ltr": ("L", 1000),
    "liter": ("L", 1000),
    "liters": ("L", 1000),
    "litre": ("L", 1000),
    "g": ("g", 1),
    "grams": ("g", 1),
    "kg": ("kg", 1000),
    "kilograms": ("kg", 1000),
    "oz": ("fl oz", 29.5735),
    "ounce": ("fl oz", 29.5735),
    "fl oz": ("fl oz", 29.5735),
    "floz": ("fl oz", 29.5735),
    "fluid ounce": ("fl oz", 29.5735),
    "fluid ounces": ("fl oz", 29.5735),
}
FORMAS_CON_BUG = {
    "litre": "BUG: '1 litre' devuelve unit='litre' y normalized=1.0 (no 1000): la rama de litros "
    "busca 'liter' dentro de la unidad y 'litre' no lo contiene (hallazgo para pi-dev)",
    "kilograms": "BUG: '2 kilograms' devuelve unit='g' y normalized=2.0 (no unit='kg', 2000): "
    "'gram' está dentro de 'kilograms' y la rama de gramos se evalúa antes (hallazgo para pi-dev)",
}

FORMAS_SIN_BUG = [f for f in FORMAS_DE_VOLUMEN if f not in FORMAS_CON_BUG]

UNIDADES_DE_CONTEO = ["wipes", "count", "sheets", "sachets", "pack", "pcs", "pieces", "pc"]

# Texto de producto sin dígitos ni símbolos que formen otro patrón; termina en espacio para
# que el número quede separado de la palabra anterior.
LETRAS = "abcdefhijkmnqrtuvwyzABCDEFHIJKMNQRTUVWYZ "
prefijos = st.text(alphabet=LETRAS, max_size=25).map(lambda s: (s.strip() + " ") if s.strip() else "")
sufijos = st.sampled_from(["", " bottle", ", refill", " - Wayakit", " (new)", "."])
mayusculas = st.sampled_from([str.lower, str.upper, str.title, lambda s: s])

# Números como aparecen en un título: entero o decimal con hasta 3 cifras, sin notación científica.
numeros_texto = st.one_of(
    st.integers(min_value=0, max_value=1_000_000).map(str),
    st.tuples(st.integers(0, 100_000), st.integers(0, 999)).map(lambda t: f"{t[0]}.{t[1]}"),
)

FRAGMENTOS = [
    "ml", "ML", "l", "L", "ltr", "liter", "litre", "liters", "g", "kg", "grams", "kilograms",
    "oz", "fl oz", "floz", "ounce", "fluid ounce", "x", "X", "Pcs", "pack of", "box of", "wipes",
    "count", "-", "/", "(", ")", ",", ".", " ", "  ", "Wayakit", "cleaner", "٣", "５",
]
# Títulos "casi realistas": mezcla de números, unidades y basura. Máx. ~200 caracteres
# (un título real no pasa de eso; el caso de miles de dígitos va aparte).
textos_arbitrarios = st.one_of(
    st.text(max_size=200),
    st.lists(
        st.one_of(st.sampled_from(FRAGMENTOS), numeros_texto, st.text(max_size=4)),
        max_size=12,
    ).map(lambda partes: " ".join(partes)[:200]),
)
# Igual, pero sin la palabra 'litre' (único caso conocido de unidad no canónica, ver FORMAS_CON_BUG).
textos_sin_litre = st.one_of(
    st.text(max_size=200),
    st.lists(
        st.one_of(st.sampled_from([f for f in FRAGMENTOS if f != "litre"]), numeros_texto, st.text(max_size=4)),
        max_size=12,
    ).map(lambda partes: " ".join(partes)[:200]),
).filter(lambda texto: "litre" not in texto.lower())


def _revisar_estructura(resultado, tipo_cantidad):
    if resultado is None:
        return
    assert set(resultado) == {"quantity", "unit", "normalized"}
    q, n = resultado["quantity"], resultado["normalized"]
    assert isinstance(q, tipo_cantidad)
    assert not (isinstance(q, float) and math.isnan(q))
    assert q >= 0
    assert n >= 0


# --- parse_volume_string ----------------------------------------------------------------------

@given(textos_arbitrarios)
def test_volumen_nunca_lanza_y_devuelve_estructura_valida(texto):
    _revisar_estructura(utils.parse_volume_string(texto), float)


@given(textos_sin_litre)
def test_volumen_unidad_siempre_canonica(texto):
    resultado = utils.parse_volume_string(texto)
    if resultado is not None:
        assert resultado["unit"] in FACTOR_POR_UNIDAD


@pytest.mark.parametrize("forma", FORMAS_SIN_BUG)
@given(numero=numeros_texto, prefijo=prefijos, sufijo=sufijos, caso=mayusculas, espacio=st.sampled_from(["", " "]))
def test_volumen_ida_y_vuelta_y_normalizacion(forma, numero, prefijo, sufijo, caso, espacio):
    """'<prefijo><q> <unidad><sufijo>' -> quantity == q, unidad canónica y normalized == q * factor."""
    unidad_esperada, factor = FORMAS_DE_VOLUMEN[forma]
    texto = f"{prefijo}{numero}{espacio}{caso(forma)}{sufijo}"
    resultado = utils.parse_volume_string(texto)
    assert resultado is not None, texto
    assert resultado["quantity"] == float(numero)
    assert resultado["unit"] == unidad_esperada
    assert resultado["normalized"] == pytest.approx(float(numero) * factor, rel=1e-12)


@pytest.mark.xfail(strict=True, reason=FORMAS_CON_BUG["litre"])
@settings(phases=[Phase.explicit])
@given(textos_arbitrarios)
@example("Wayakit cleaner 1 litre")
def test_bug_volumen_litre_unidad_no_canonica(texto):
    resultado = utils.parse_volume_string(texto)
    assert resultado["unit"] == "L" and resultado["normalized"] == 1000.0, resultado


@pytest.mark.xfail(strict=True, reason=FORMAS_CON_BUG["kilograms"])
@settings(phases=[Phase.explicit])
@given(numero=numeros_texto)
@example(numero="2")
def test_bug_volumen_kilograms_se_lee_como_gramos(numero):
    resultado = utils.parse_volume_string(f"Powder {numero} kilograms")
    assert resultado["unit"] == "kg" and resultado["normalized"] == float(numero) * 1000, resultado


@given(numero=numeros_texto, forma=st.sampled_from(sorted(FORMAS_DE_VOLUMEN)))
def test_volumen_relaciones_entre_unidades(numero, forma):
    """L = 1000 ml, kg = 1000 g, fl oz = 29.5735 ml, sobre la salida canónica."""
    resultado = utils.parse_volume_string(f"{numero} {forma}")
    if resultado is None or resultado["unit"] not in FACTOR_POR_UNIDAD:
        return  # la forma 'litre' ya está cubierta por test_bug_volumen_litre_unidad_no_canonica
    assert resultado["normalized"] == pytest.approx(
        resultado["quantity"] * FACTOR_POR_UNIDAD[resultado["unit"]], rel=1e-12
    )


# --- parse_count_string ------------------------------------------------------------------------

@given(textos_arbitrarios)
def test_conteo_nunca_lanza_y_devuelve_estructura_valida(texto):
    resultado = utils.parse_count_string(texto)
    _revisar_estructura(resultado, int)
    if resultado is not None:
        assert resultado["unit"] == "units"
        assert resultado["normalized"] == resultado["quantity"]


@given(
    n=st.integers(0, 1_000_000),
    unidad=st.sampled_from(UNIDADES_DE_CONTEO),
    humedas=st.sampled_from(["", "wet ", "Wet "]),
    prefijo=prefijos,
    sufijo=sufijos,
    caso=mayusculas,
    espacio=st.sampled_from(["", " "]),
)
def test_conteo_ida_y_vuelta_n_unidades(n, unidad, humedas, prefijo, sufijo, caso, espacio):
    texto = f"{prefijo}{n}{espacio}{humedas}{caso(unidad)}{sufijo}"
    resultado = utils.parse_count_string(texto)
    assert resultado == {"quantity": n, "unit": "units", "normalized": n}, texto


@given(
    n=st.integers(0, 1_000_000),
    envase=st.sampled_from(["pack", "box", "Pack", "BOX"]),
    prefijo=prefijos,
    sufijo=sufijos,
)
def test_conteo_ida_y_vuelta_pack_of_n(n, envase, prefijo, sufijo):
    texto = f"{prefijo}{envase} of {n}{sufijo}"
    assert utils.parse_count_string(texto) == {"quantity": n, "unit": "units", "normalized": n}, texto


@given(n=st.integers(0, 1_000_000), envase=st.sampled_from(["box", "pack", "count"]), espacio=st.sampled_from(["", " "]))
def test_conteo_ida_y_vuelta_n_por_envase(n, envase, espacio):
    texto = f"{n}{espacio}/{espacio}{envase}"
    assert utils.parse_count_string(texto)["quantity"] == n


# --- parse_saco_count_string -------------------------------------------------------------------

@given(textos_arbitrarios)
def test_saco_nunca_lanza_y_devuelve_estructura_valida(texto):
    resultado = utils.parse_saco_count_string(texto)
    _revisar_estructura(resultado, int)
    if resultado is not None:
        assert resultado["unit"] == "units"
        assert resultado["normalized"] == resultado["quantity"]


@given(
    n=st.integers(0, 1_000_000),
    palabra=st.sampled_from(["piece", "wipes", "rags"]),
    caso=mayusculas,
    izq=st.sampled_from(["", " "]),
    der=st.sampled_from(["", " "]),
    prefijo=prefijos,
)
def test_saco_ida_y_vuelta(n, palabra, caso, izq, der, prefijo):
    texto = f"{prefijo}{n}{izq}-{der}{caso(palabra)}"
    assert utils.parse_saco_count_string(texto) == {"quantity": n, "unit": "units", "normalized": n}


# --- parse_volume_with_multiplier --------------------------------------------------------------

UNIDADES_MULTIPLICADOR = {"ml": 1, "milliliters": 1, "l": 1000, "ltr": 1000, "liter": 1000, "liters": 1000}
# Decimales con 1 a 3 cifras y >= 0.001: m*q nunca baja de 1e-4 ni llega a 1e16, que es donde
# Python escribe floats en notación científica (ese caso tiene su propio xfail en unit/).
cantidades_base = st.one_of(
    st.integers(0, 100_000).map(str),
    st.tuples(st.integers(0, 10_000), st.integers(1, 999)).map(lambda t: f"{t[0]}.{t[1]:03d}"),
)


@given(textos_arbitrarios)
def test_multiplicador_nunca_lanza_y_es_consistente(texto):
    """Si devuelve algo, normalized == quantity * factor de su unidad (además de la estructura)."""
    resultado = utils.parse_volume_with_multiplier(texto)
    _revisar_estructura(resultado, float)
    if resultado is not None and resultado["unit"] in FACTOR_POR_UNIDAD:
        assert resultado["normalized"] == pytest.approx(
            resultado["quantity"] * FACTOR_POR_UNIDAD[resultado["unit"]], rel=1e-9
        )


@given(
    m=st.integers(1, 48),
    q=cantidades_base,
    unidad=st.sampled_from(sorted(UNIDADES_MULTIPLICADOR)),
    formato=st.sampled_from(["simple", "officesupply", "gogreen"]),
    x=st.sampled_from(["x", "X"]),
)
def test_multiplicador_total_es_m_por_q(m, q, unidad, formato, x):
    """'6 x 500 ml', '(6 Bottles x 500 ml)' y '6 Pcs x 500 ml' -> quantity 3000 y normalized coherente."""
    plantilla = {
        "simple": "Cleaner {m} {x} {q} {u}",
        "officesupply": "Cleaner ({m} Bottles {x} {q} {u})",
        "gogreen": "Cleaner {m} Pcs {x} {q} {u}",
    }[formato]
    texto = plantilla.format(m=m, x=x, q=q, u=unidad)
    resultado = utils.parse_volume_with_multiplier(texto)
    assert resultado is not None, texto
    total = m * float(q)
    assert resultado["quantity"] == pytest.approx(total, rel=1e-12)
    assert resultado["normalized"] == pytest.approx(total * UNIDADES_MULTIPLICADOR[unidad], rel=1e-9)


# --- extract_aerosense_units -------------------------------------------------------------------

@given(textos_arbitrarios)
def test_aerosense_nunca_lanza(texto):
    resultado = utils.extract_aerosense_units(texto)
    assert resultado is None or (isinstance(resultado, int) and resultado >= 0)


@given(n=st.integers(0, 100_000), prefijo=prefijos)
def test_aerosense_pcs_entre_parentesis(n, prefijo):
    assert utils.extract_aerosense_units(f"{prefijo}({n} pcs)") == n


@given(a=st.integers(0, 1000), b=st.integers(0, 1000), prefijo=prefijos, espacio=st.sampled_from(["", " "]))
def test_aerosense_pack_por_unidades_es_producto(a, b, prefijo, espacio):
    assert utils.extract_aerosense_units(f"{prefijo}{a}-pack{espacio}x{espacio}{b}") == a * b
