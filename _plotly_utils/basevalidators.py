import base64
import numbers
import textwrap
import uuid
from importlib import import_module

import io
from copy import deepcopy

import numpy as np
import pandas as pd
import re

# Utility functions
# -----------------
def copy_to_contiguous_readonly_numpy_array(v, dtype=None, force_numeric=False):

    # Copy to numpy array and handle dtype param
    # ------------------------------------------
    # If dtype was not specified then it will be passed to the numpy array constructor as None and the data type
    # will be inferred automatically

    # TODO: support datetime dtype here and in widget serialization
    numeric_kinds = ['u', 'i', 'f']

    if not isinstance(v, np.ndarray):
        new_v = np.array(v, order='C', dtype=dtype)
    elif v.dtype.kind in numeric_kinds:
        new_v = np.ascontiguousarray(v.astype(dtype))
    else:
        new_v = v.copy()

    # Handle force numeric param
    # --------------------------
    if force_numeric and new_v.dtype.kind not in numeric_kinds:  # (un)signed int, or float
        raise ValueError('Input value is not numeric and force_numeric parameter set to True')

    if dtype != 'unicode':
        # Force non-numeric arrays to have object type
        # --------------------------------------------
        # Here we make sure that non-numeric arrays have the object datatype. This works around cases like
        # np.array([1, 2, '3']) where numpy converts the integers to strings and returns array of dtype '<U21'
        if new_v.dtype.kind not in ['u', 'i', 'f', 'O']:  # (un)signed int, float, or object
            new_v = np.array(v, dtype='object')

    # Convert int64 arrays to int32
    # -----------------------------
    # JavaScript doesn't support int64 typed arrays
    if new_v.dtype == 'int64':
        new_v = new_v.astype('int32')

    # Set new array to be read-only
    # -----------------------------
    new_v.flags['WRITEABLE'] = False

    return new_v


def is_array(v):
    return (isinstance(v, (list, tuple)) or
            (isinstance(v, np.ndarray) and v.ndim == 1) or
            isinstance(v, pd.Series))


def type_str(v):

    if isinstance(v, str) and v.startswith('<class '):
        return repr(v[7:-1])

    if not isinstance(v, type):
        v = type(v)

    return "'{module}.{name}'".format(module=v.__module__, name=v.__name__)


# Validators
# ----------
class BaseValidator:
    def __init__(self, plotly_name, parent_name, role=None, **_):
        self.parent_name = parent_name
        self.plotly_name = plotly_name
        self.role = role

    def validate_coerce(self, v):
        raise NotImplementedError()

    def description(self):
        """Returns a string that describes the values that are acceptable to the validator.

        Should start with:
            The '{plotly_name}' property is a...

        For consistancy, string should have leading 4-space indent
        """
        raise NotImplementedError()

    def raise_invalid_val(self, v):
        raise ValueError(
            ("\n"
             "    Invalid value of type {typ} received for the '{plotly_name}' property of {parent_name}\n"
             "        Received value: {v}\n\n"
             "{vald_clr_desc}\n"
             ).format(plotly_name=self.plotly_name,
                      parent_name=self.parent_name,
                      typ=type_str(v),
                      v=repr(v),
                      vald_clr_desc=self.description()))

    def raise_invalid_elements(self, invalid_els):
        if invalid_els:
            raise ValueError(
                ("\n"
                 "    Invalid element(s) received for the '{plotly_name}' property of {parent_name}\n"
                 "        Invalid elements include: {invalid}\n\n"
                 "{vald_clr_desc}\n"
                 ).format(plotly_name=self.plotly_name,
                          parent_name=self.parent_name,
                          invalid=invalid_els[:10],
                          vald_clr_desc=self.description()))


class DataArrayValidator(BaseValidator):
    """
        "data_array": {
            "description": "An {array} of data. The value MUST be an {array}, or we ignore it.",
            "requiredOpts": [],
            "otherOpts": [
                "dflt"
            ]
        },
    """

    def __init__(self, plotly_name, parent_name, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

    def description(self):
        return ("""\
    The '{plotly_name}' property is an array that may be specified as a tuple, list, or one-dimensional numpy array"""
                .format(plotly_name=self.plotly_name))

    def validate_coerce(self, v):

        if v is None:
            # Pass None through
            pass
        elif is_array(v):
            v = copy_to_contiguous_readonly_numpy_array(v)
        else:
            self.raise_invalid_val(v)
        return v


class EnumeratedValidator(BaseValidator):
    """
        "enumerated": {
            "description": "Enumerated value type. The available values are listed in `values`.",
            "requiredOpts": [
                "values"
            ],
            "otherOpts": [
                "dflt",
                "coerceNumber",
                "arrayOk"
            ]
        },
    """
    def __init__(self, plotly_name, parent_name, values,
                 array_ok=False,
                 coerce_number=False,
                 **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

        # coerce_number is rarely used and not implemented
        self.coerce_number = coerce_number
        self.values = values

        # compile regexes
        self.val_regexs = []

        # regex replacement that runs before the matching regex
        # So far, this is only used to cast x1 -> x for anchor-style
        # enumeration properties
        self.regex_replacements = []
        for v in self.values:
            if v and isinstance(v, str) and v[0] == '/' and v[-1] == '/':
                # String is regex with leading and trailing '/' character
                regex_str = v[1:-1]
                self.val_regexs.append(re.compile(regex_str))
                self.regex_replacements.append(
                    EnumeratedValidator.build_regex_replacement(regex_str))
            else:
                self.val_regexs.append(None)
                self.regex_replacements.append(None)

        self.array_ok = array_ok

    @staticmethod
    def build_regex_replacement(regex_str):
        # regex_str = r"^y([2-9]|[1-9][0-9]+)?$"

        # Remove id of 1 from subplotid-style anchors. The regular
        # expressions forbid a suffix of 1. But we want just want to convert
        # to by removing the 1 (e.g. turn x1 -> x).
        #
        # To be cautious, we only perform this conversion for enumerated
        # values that match the anchor-style regex
        match = re.match(r"\^(\w)\(\[2\-9\]\|\[1\-9\]\[0\-9\]\+\)\?\$",
                         regex_str)

        if match:
            anchor_char = match.group(1)
            return '^' + anchor_char + '1$', anchor_char
        else:
            return None


    def perform_replacemenet(self, v):
        for repl_args in self.regex_replacements:
            if repl_args:
                v = re.sub(repl_args[0], repl_args[1], v)

        return v

    def description(self):

        # Separate regular values from regular expressions
        enum_vals = []
        enum_regexs = []
        for v, regex in zip(self.values, self.val_regexs):
            if regex is not None:
                enum_regexs.append(regex.pattern)
            else:
                enum_vals.append(v)

        desc = """\
    The '{plotly_name}' property is an enumeration that may be specified as:""".format(plotly_name=self.plotly_name)

        if enum_vals:
            enum_vals_str = '\n'.join(textwrap.wrap(repr(enum_vals),
                                                    subsequent_indent=' ' * 12,
                                                    break_on_hyphens=False))

            desc = desc + """
      - One of the following enumeration values:
            {enum_vals_str}""".format(enum_vals_str=enum_vals_str)


        if enum_regexs:
            enum_regexs_str = '\n'.join(textwrap.wrap(repr(enum_regexs),
                                                    subsequent_indent=' ' * 12,
                                                    break_on_hyphens=False))

            desc = desc + """
      - A string that matches one of the following regular expressions:
            {enum_vals_str}""".format(enum_vals_str=enum_regexs_str)

        if self.array_ok:
            desc = desc + """
      - A tuple, list, or one-dimensional numpy array of the above"""

        return desc

    def in_values(self, e):
        is_str = isinstance(e, str)
        for v, regex in zip(self.values, self.val_regexs):
            if is_str and regex:
                in_values = regex.fullmatch(e) is not None
            else:
                in_values = e == v

            if in_values:
                return True

        return False

    def validate_coerce(self, v):
        if v is None:
            # Pass None through
            pass
        elif self.array_ok and is_array(v):
            v = [self.perform_replacemenet(v_el) for v_el in v]

            invalid_els = [e for e in v if (not self.in_values(e))]
            if invalid_els:
                self.raise_invalid_elements(invalid_els)

            v = copy_to_contiguous_readonly_numpy_array(v)
        else:
            v = self.perform_replacemenet(v)
            if not self.in_values(v):
                self.raise_invalid_val(v)
        return v


class BooleanValidator(BaseValidator):
    """
        "boolean": {
            "description": "A boolean (true/false) value.",
            "requiredOpts": [],
            "otherOpts": [
                "dflt"
            ]
        },
    """
    def __init__(self, plotly_name, parent_name, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

    def description(self):
        return ("""\
    The '{plotly_name}' property must be specified as a bool (either True, or False)"""
                .format(plotly_name=self.plotly_name))

    def validate_coerce(self, v):
        if v is None:
            # Pass None through
            pass
        elif not isinstance(v, bool):
            self.raise_invalid_val(v)

        return v


class NumberValidator(BaseValidator):
    """
        "number": {
            "description": "A number or a numeric value (e.g. a number inside a string). When applicable, values greater (less) than `max` (`min`) are coerced to the `dflt`.",
            "requiredOpts": [],
            "otherOpts": [
                "dflt",
                "min",
                "max",
                "arrayOk"
            ]
        },
    """
    def __init__(self, plotly_name, parent_name,
                 min=None, max=None, array_ok=False, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

        # Handle min
        if min is None and max is not None:
            # Max was specified, so make min -inf
            self.min_val = -np.inf
        else:
            self.min_val = min

        # Handle max
        if max is None and min is not None:
            # Min was specified, so make min inf
            self.max_val = np.inf
        else:
            self.max_val = max

        self.array_ok = array_ok

    def description(self):
        desc = """\
    The '{plotly_name}' property is a number and may be specified as:""".format(plotly_name=self.plotly_name)

        if self.min_val is None and self.max_val is None:
            desc = desc + """
      - An int or float"""

        else:
            desc = desc + """
      - An int or float in the interval [{min_val}, {max_val}]""".format(
                min_val=self.min_val,
                max_val=self.max_val)

        if self.array_ok:
            desc = desc + """
      - A tuple, list, or one-dimensional numpy array of the above"""

        return desc

    def validate_coerce(self, v):
        if v is None:
            # Pass None through
            pass
        elif self.array_ok and is_array(v):

            try:
                v_array = copy_to_contiguous_readonly_numpy_array(v, force_numeric=True)
            except ValueError as ve:
                self.raise_invalid_val(v)

            v_valid = np.ones(v_array.shape, dtype='bool')
            if self.min_val is not None:
                v_valid = np.logical_and(v_valid, v_array >= self.min_val)

            if self.max_val is not None:
                v_valid = np.logical_and(v_valid, v_array <= self.max_val)

            if not np.all(v_valid):
                # Grab up to the first 10 invalid values
                some_invalid_els = np.array(v, dtype='object')[np.logical_not(v_valid)][:10].tolist()
                self.raise_invalid_elements(some_invalid_els)

            v = v_array  # Always numpy array of float64
        else:
            if not isinstance(v, numbers.Number):
                self.raise_invalid_val(v)

            if (self.min_val is not None and not v >= self.min_val) or \
                    (self.max_val is not None and not v <= self.max_val):

                self.raise_invalid_val(v)

        return v


class IntegerValidator(BaseValidator):
    """
        "integer": {
            "description": "An integer or an integer inside a string. When applicable, values greater (less) than `max` (`min`) are coerced to the `dflt`.",
            "requiredOpts": [],
            "otherOpts": [
                "dflt",
                "min",
                "max",
                "arrayOk"
            ]
        },
    """
    def __init__(self, plotly_name, parent_name,
                 min=None, max=None, array_ok=False, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

        # Handle min
        if min is None and max is not None:
            # Max was specified, so make min -inf
            self.min_val = np.iinfo(np.int32).min
        else:
            self.min_val = min

        # Handle max
        if max is None and min is not None:
            # Min was specified, so make min inf
            self.max_val = np.iinfo(np.int32).max
        else:
            self.max_val = max

        self.array_ok = array_ok

    def description(self):
        desc = """\
    The '{plotly_name}' property is a integer and may be specified as:""".format(plotly_name=self.plotly_name)

        if self.min_val is None and self.max_val is None:
            desc = desc + """
      - An int (or float that will be cast to an int)"""
        else:
            desc = desc + """
      - An int (or float that will be cast to an int) in the interval [{min_val}, {max_val}]""".format(
                min_val=self.min_val,
                max_val=self.max_val)

        if self.array_ok:
            desc = desc + """
      - A tuple, list, or one-dimensional numpy array of the above"""

        return desc

    def validate_coerce(self, v):
        if v is None:
            # Pass None through
            pass
        elif self.array_ok and is_array(v):

            try:
                v_array = copy_to_contiguous_readonly_numpy_array(v, dtype='int32')

            except (ValueError, TypeError, OverflowError) as ve:
                self.raise_invalid_val(v)

            v_valid = np.ones(v_array.shape, dtype='bool')
            if self.min_val is not None:
                v_valid = np.logical_and(v_valid, v_array >= self.min_val)

            if self.max_val is not None:
                v_valid = np.logical_and(v_valid, v_array <= self.max_val)

            if not np.all(v_valid):
                invalid_els = np.array(v, dtype='object')[np.logical_not(v_valid)][:10].tolist()
                self.raise_invalid_elements(invalid_els)

            v = v_array
        else:
            try:
                if not isinstance(v, numbers.Number):
                    # don't let int() cast strings to ints
                    self.raise_invalid_val(v)

                v_int = int(v)
            except (ValueError, TypeError, OverflowError) as ve:
                self.raise_invalid_val(v)

            if (self.min_val is not None and not v >= self.min_val) or \
                    (self.max_val is not None and not v <= self.max_val):
                self.raise_invalid_val(v)

            v = v_int

        return v


class StringValidator(BaseValidator):
    """
        "string": {
            "description": "A string value. Numbers are converted to strings except for attributes with `strict` set to true.",
            "requiredOpts": [],
            "otherOpts": [
                "dflt",
                "noBlank",
                "strict",
                "arrayOk",
                "values"
            ]
        },
    """
    def __init__(self, plotly_name, parent_name,
                 no_blank=False, strict=False, array_ok=False, values=None,
                 **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)
        self.no_blank = no_blank
        self.strict = strict        # Not implemented. We're always strict
        self.array_ok = array_ok
        self.values = values

    def description(self):
        desc = """\
    The '{plotly_name}' property is a string and must be specified as:""".format(plotly_name=self.plotly_name)

        if self.no_blank:
            desc = desc + """
      - A non-empty string"""
        elif self.values:
            valid_str = '\n'.join(textwrap.wrap(repr(self.values),
                                                subsequent_indent=' ' * 12,
                                                break_on_hyphens=False))

            desc = desc + """
      - One of the following strings: 
            {valid_str}""".format(valid_str=valid_str)
        else:
            desc = desc + """
      - A string"""

        if self.array_ok:
            desc = desc + """
      - A tuple, list, or one-dimensional numpy array of the above"""

        return desc

    def validate_coerce(self, v):
        if v is None:
            # Pass None through
            pass
        elif self.array_ok and is_array(v):

            # Make sure all elements are strings. Is there a more efficient way to do this in numpy?
            invalid_els = [e for e in v if not isinstance(e, str)]
            if invalid_els:
                self.raise_invalid_elements(invalid_els)

            v = copy_to_contiguous_readonly_numpy_array(v, dtype='unicode')

            if self.no_blank:
                invalid_els = v[v == ''][:10].tolist()
                if invalid_els:
                    self.raise_invalid_elements(invalid_els)

            if self.values:
                invalid_els = v[np.logical_not(np.isin(v, self.values))][:10].tolist()
                if invalid_els:
                    self.raise_invalid_elements(invalid_els)
        else:
            if not isinstance(v, str):
                self.raise_invalid_val(v)

            if self.no_blank and len(v) == 0:
                self.raise_invalid_val(v)

            if self.values and v not in self.values:
                self.raise_invalid_val(v)

        return v


class ColorValidator(BaseValidator):
    """
        "color": {
            "description": "A string describing color. Supported formats: - hex (e.g. '#d3d3d3') - rgb (e.g. 'rgb(255, 0, 0)') - rgba (e.g. 'rgb(255, 0, 0, 0.5)') - hsl (e.g. 'hsl(0, 100%, 50%)') - hsv (e.g. 'hsv(0, 100%, 100%)') - named colors (full list: http://www.w3.org/TR/css3-color/#svg-color)",
            "requiredOpts": [],
            "otherOpts": [
                "dflt",
                "arrayOk"
            ]
        },
    """
    re_hex = re.compile('#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{3})')
    re_rgb_etc = re.compile('(rgb|hsl|hsv)a?\([\d.]+%?(,[\d.]+%?){2,3}\)')

    named_colors = [
        "aliceblue", "antiquewhite", "aqua", "aquamarine", "azure", "beige", "bisque", "black", "blanchedalmond",
        "blue", "blueviolet", "brown", "burlywood", "cadetblue", "chartreuse", "chocolate", "coral", "cornflowerblue",
        "cornsilk", "crimson", "cyan", "darkblue", "darkcyan", "darkgoldenrod", "darkgray", "darkgrey", "darkgreen",
        "darkkhaki", "darkmagenta", "darkolivegreen", "darkorange", "darkorchid", "darkred", "darksalmon",
        "darkseagreen", "darkslateblue", "darkslategray", "darkslategrey", "darkturquoise", "darkviolet", "deeppink",
        "deepskyblue", "dimgray", "dimgrey", "dodgerblue", "firebrick", "floralwhite", "forestgreen", "fuchsia",
        "gainsboro", "ghostwhite", "gold", "goldenrod", "gray", "grey", "green", "greenyellow", "honeydew", "hotpink",
        "indianred", "indigo", "ivory", "khaki", "lavender", "lavenderblush", "lawngreen", "lemonchiffon", "lightblue",
        "lightcoral", "lightcyan", "lightgoldenrodyellow", "lightgray", "lightgrey", "lightgreen", "lightpink",
        "lightsalmon", "lightseagreen", "lightskyblue", "lightslategray", "lightslategrey", "lightsteelblue",
        "lightyellow", "lime", "limegreen", "linen", "magenta", "maroon", "mediumaquamarine", "mediumblue",
        "mediumorchid", "mediumpurple", "mediumseagreen", "mediumslateblue", "mediumspringgreen", "mediumturquoise",
        "mediumvioletred", "midnightblue", "mintcream", "mistyrose", "moccasin", "navajowhite", "navy", "oldlace",
        "olive", "olivedrab", "orange", "orangered", "orchid", "palegoldenrod", "palegreen", "paleturquoise",
        "palevioletred", "papayawhip", "peachpuff", "peru", "pink", "plum", "powderblue", "purple", "red", "rosybrown",
        "royalblue", "saddlebrown", "salmon", "sandybrown", "seagreen", "seashell", "sienna", "silver", "skyblue",
        "slateblue", "slategray", "slategrey", "snow", "springgreen", "steelblue", "tan", "teal", "thistle", "tomato",
        "turquoise", "violet", "wheat", "white", "whitesmoke", "yellow", "yellowgreen"]

    def __init__(self, plotly_name, parent_name,
                 array_ok=False, colorscale_path=None, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)
        self.colorscale_path = colorscale_path
        self.array_ok = array_ok

    def numbers_allowed(self):
        return self.colorscale_path is not None

    def description(self):

        named_clrs_str = '\n'.join(textwrap.wrap(', '.join(
            self.named_colors), width=79, subsequent_indent=' ' * 12))

        valid_color_description = """\
    The '{plotly_name}' property is a color and may be specified as:  
      - A hex string (e.g. '#ff0000')
      - An rgb/rgba string (e.g. 'rgb(255,0,0)')
      - An hsl/hsla string (e.g. 'hsl(0,100%,50%)')
      - An hsv/hsva string (e.g. 'hsv(0,100%,100%)')
      - A named CSS color:
            {clrs}""".format(
            plotly_name=self.plotly_name,
            clrs=named_clrs_str)

        if self.colorscale_path:
            valid_color_description = valid_color_description + """
      - A number that will be interpreted as a color according to {colorscale_path}""".format(
                colorscale_path=self.colorscale_path)

        if self.array_ok:
            valid_color_description = valid_color_description + """
      - A list or array of any of the above"""

        return valid_color_description

    def validate_coerce(self, v):
        if v is None:
            # Pass None through
            pass
        elif self.array_ok and is_array(v):
            v_array = copy_to_contiguous_readonly_numpy_array(v)
            if self.numbers_allowed() and v_array.dtype.kind in ['u', 'i', 'f']:  # (un)signed int or float
                # All good
                v = v_array
            else:
                validated_v = [ColorValidator.perform_validate_coerce(e, allow_number=self.numbers_allowed())
                               for e in v]

                invalid_els = [el for el, validated_el in zip(v, validated_v) if validated_el is None]
                if invalid_els:
                    self.raise_invalid_elements(invalid_els)

                # ### Check that elements have valid colors types ###
                if self.numbers_allowed():
                    v = copy_to_contiguous_readonly_numpy_array(validated_v, dtype='object')
                else:
                    v = copy_to_contiguous_readonly_numpy_array(validated_v, dtype='unicode')

        else:
            # Validate scalar color
            validated_v = ColorValidator.perform_validate_coerce(v, allow_number=self.numbers_allowed())
            if validated_v is None:
                self.raise_invalid_val(v)

            v = validated_v

        return v

    @staticmethod
    def perform_validate_coerce(v, allow_number=None):

        if isinstance(v, numbers.Number) and allow_number:
            # If allow_numbers then any number is ok
            return v
        elif not isinstance(v, str):
            return None
        else:
            # Remove spaces so regexes don't need to bother with them.
            v = v.replace(' ', '')
            v = v.lower()

            if ColorValidator.re_hex.fullmatch(v):
                # valid hex color (e.g. #f34ab3)
                return v
            elif ColorValidator.re_rgb_etc.fullmatch(v):
                # Valid rgb(a), hsl(a), hsv(a) color (e.g. rgba(10, 234, 200, 50%)
                return v
            elif v in ColorValidator.named_colors:
                # Valid named color (e.g. 'coral')
                return v
            else:
                # Not a valid color
                return None


class ColorlistValidator(BaseValidator):
    """
        "colorlist": {
          "description": "A list of colors. Must be an {array} containing valid colors.",
          "requiredOpts": [],
          "otherOpts": [
            "dflt"
          ]
        }
    """
    def __init__(self, plotly_name, parent_name, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

    def description(self):
        return ("""\
    The '{plotly_name}' property is a colorlist that may be specified as a tuple, list, 
    or one-dimensional numpy array of valid color strings""".format(plotly_name=self.plotly_name))

    def validate_coerce(self, v):

        if v is None:
            # Pass None through
            pass
        elif is_array(v):
            validated_v = [ColorValidator.perform_validate_coerce(e, allow_number=False) for e in v]

            invalid_els = [el for el, validated_el in zip(v, validated_v) if validated_el is None]
            if invalid_els:
                self.raise_invalid_elements(invalid_els)

            v = copy_to_contiguous_readonly_numpy_array(validated_v, dtype='unicode')
        else:
            self.raise_invalid_val(v)
        return v


class ColorscaleValidator(BaseValidator):
    """
        "colorscale": {
            "description": "A Plotly colorscale either picked by a name: (any of Greys, YlGnBu, Greens, YlOrRd, Bluered, RdBu, Reds, Blues, Picnic, Rainbow, Portland, Jet, Hot, Blackbody, Earth, Electric, Viridis ) customized as an {array} of 2-element {arrays} where the first element is the normalized color level value (starting at *0* and ending at *1*), and the second item is a valid color string.",
            "requiredOpts": [],
            "otherOpts": [
                "dflt"
            ]
        },
    """

    named_colorscales = ['Greys', 'YlGnBu', 'Greens', 'YlOrRd', 'Bluered', 'RdBu', 'Reds', 'Blues', 'Picnic',
                         'Rainbow', 'Portland', 'Jet', 'Hot', 'Blackbody', 'Earth', 'Electric', 'Viridis']

    def __init__(self, plotly_name, parent_name, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

    def description(self):
        desc = """\
    The '{plotly_name}' property is a colorscale and may be specified as:
      - A list of 2-element lists where the first element is the normalized color level value 
        (starting at 0 and ending at 1), and the second item is a valid color string. 
        (e.g. [[0.5, 'red'], [1.0, 'rgb(0, 0, 255)']])
      - One of the following named colorscales:
            ['Greys', 'YlGnBu', 'Greens', 'YlOrRd', 'Bluered', 'RdBu', 'Reds', 'Blues', 'Picnic', 
             'Rainbow', 'Portland', 'Jet', 'Hot', 'Blackbody', 'Earth', 'Electric', 'Viridis']
        """.format(plotly_name=self.plotly_name)

        return desc

    def validate_coerce(self, v):
        v_valid = False

        if v is None:
            # Pass None through
            pass
        if v is None:
            v_valid = True
        elif isinstance(v, str):
            v_match = [el for el in ColorscaleValidator.named_colorscales if el.lower() == v.lower()]
            if v_match:
                v_valid = True
                v = v_match[0]

        elif is_array(v) and len(v) > 0:
            invalid_els = [e for e in v
                           if not is_array(e) or
                           len(e) != 2 or
                           not isinstance(e[0], numbers.Number) or
                           not (0 <= e[0] <= 1) or
                           not isinstance(e[1], str) or
                           ColorValidator.perform_validate_coerce(e[1]) is None]
            if len(invalid_els) == 0:
                v_valid = True

                # Convert to tuple of tuples so colorscale is immutable
                v = tuple([tuple([e[0], ColorValidator.perform_validate_coerce(e[1])]) for e in v])

        if not v_valid:
            self.raise_invalid_val(v)
        return v


class AngleValidator(BaseValidator):
    """
        "angle": {
            "description": "A number (in degree) between -180 and 180.",
            "requiredOpts": [],
            "otherOpts": [
                "dflt"
            ]
        },
    """
    def __init__(self, plotly_name, parent_name, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

    def description(self):
        desc = """\
    The '{plotly_name}' property is a angle (in degrees) that may be specified as a number between -180 and 180.
    Numeric values outside this range are converted to the equivalent value (e.g. 270 is converted to -90).
        """.format(plotly_name=self.plotly_name)

        return desc

    def validate_coerce(self, v):
        if v is None:
            # Pass None through
            pass
        elif not isinstance(v, numbers.Number):
            self.raise_invalid_val(v)
        else:
            # Normalize v onto the interval [-180, 180)
            v = (v + 180) % 360 - 180

        return v


class SubplotidValidator(BaseValidator):
    """
        "subplotid": {
            "description": "An id string of a subplot type (given by dflt), optionally followed by an integer >1. e.g. if dflt='geo', we can have 'geo', 'geo2', 'geo3', ...",
            "requiredOpts": [
                "dflt"
            ],
            "otherOpts": []
        },
    """
    def __init__(self, plotly_name, parent_name, dflt, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)
        self.base = dflt
        self.regex = dflt + "(\d*)"

    def description(self):

        desc = """\
    The '{plotly_name}' property is an identifier of a particular subplot, of type '{base}', that 
    may be specified as the string '{base}' optionally followed by an integer >= 1 
    (e.g. '{base}', '{base}1', '{base}2', '{base}3', etc.)
        """.format(plotly_name=self.plotly_name, base=self.base)
        return desc

    def validate_coerce(self, v):
        if v is None:
            pass
        elif not isinstance(v, str):
            self.raise_invalid_val(v)
        else:
            match = re.fullmatch(self.regex, v)
            if not match:
                is_valid = False
            else:
                digit_str = match.group(1)
                if len(digit_str) > 0 and int(digit_str) == 0:
                    is_valid = False
                elif len(digit_str) > 0 and int(digit_str) == 1:
                    # Remove 1 suffix (e.g. x1 -> x)
                    v = self.base
                    is_valid = True
                else:
                    is_valid = True

            if not is_valid:
                self.raise_invalid_val(v)
        return v


class FlaglistValidator(BaseValidator):
    """
        "flaglist": {
            "description": "A string representing a combination of flags (order does not matter here). Combine any of the available `flags` with *+*. (e.g. ('lines+markers')). Values in `extras` cannot be combined.",
            "requiredOpts": [
                "flags"
            ],
            "otherOpts": [
                "dflt",
                "extras",
                "arrayOk"
            ]
        },
    """
    def __init__(self, plotly_name, parent_name, flags,
                 extras=None, array_ok=False, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)
        self.flags = flags
        self.extras = extras if extras is not None else []
        self.array_ok = array_ok

        self.all_flags = self.flags + self.extras

    def description(self):

        desc = ("""\
    The '{plotly_name}' property is a flaglist and may be specified as a string containing:"""
                ).format(plotly_name=self.plotly_name)

        # Flags
        desc = desc + ("""
      - Any combination of {flags} joined with '+' characters (e.g. '{eg_flag}')"""
                       ).format(flags=self.flags,
                                eg_flag='+'.join(self.flags[:2]))

        # Extras
        if self.extras:
            desc = desc + ("""
        OR exactly one of {extras} (e.g. '{eg_extra}')"""
                           ).format(extras=self.extras,
                                    eg_extra=self.extras[-1])

        if self.array_ok:
            desc = desc  + """
      - A list or array of the above"""

        return desc

    def perform_validate_coerce(self, v):
        if not isinstance(v, str):
            return None

        split_vals = [e.strip() for e in re.split('[,+]', v)]

        all_flags_valid = [f for f in split_vals if f not in self.all_flags] == []
        has_extras = [f for f in split_vals if f in self.extras] != []

        is_valid = all_flags_valid and (not has_extras or len(split_vals) == 1)
        if is_valid:
            return '+'.join(split_vals)
        else:
            return None

    def validate_coerce(self, v):
        if v is None:
            # Pass None through
            pass
        elif self.array_ok and is_array(v):

            validated_v = [self.perform_validate_coerce(e) for e in v]  # Coerce individual strings

            invalid_els = [el for el, validated_el in zip(v, validated_v) if validated_el is None]
            if invalid_els:
                self.raise_invalid_elements(invalid_els)

            v = copy_to_contiguous_readonly_numpy_array(validated_v, dtype='unicode')
        else:

            validated_v = self.perform_validate_coerce(v)
            if validated_v is None:
                self.raise_invalid_val(v)

            v = validated_v

        return v


class AnyValidator(BaseValidator):
    """
        "any": {
            "description": "Any type.",
            "requiredOpts": [],
            "otherOpts": [
                "dflt",
                "values",
                "arrayOk"
            ]
        },
    """
    def __init__(self, plotly_name, parent_name,
                 values=None, array_ok=False, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)
        self.values = values
        self.array_ok = array_ok

    def description(self):

        desc = """\
    The '{plotly_name}' property accepts values of any type
        """.format(plotly_name=self.plotly_name)
        return desc

    def validate_coerce(self, v):
        if v is None:
            # Pass None through
            pass
        elif self.array_ok and is_array(v):
            v = copy_to_contiguous_readonly_numpy_array(v, dtype='object')

        return v


class InfoArrayValidator(BaseValidator):
    """
        "info_array": {
            "description": "An {array} of plot information.",
            "requiredOpts": [
                "items"
            ],
            "otherOpts": [
                "dflt",
                "freeLength"
            ]
        }
    """
    def __init__(self, plotly_name, parent_name,
                 items, free_length=None, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)
        self.items = items

        self.item_validators = []
        info_array_items = (self.items if isinstance(self.items, list)
                            else [self.items])

        for i, item in enumerate(info_array_items):
            item_validator = InfoArrayValidator.build_validator(
                item, '{plotly_name}[{i}]'
                    .format(plotly_name=plotly_name, i=i), parent_name)
            self.item_validators.append(item_validator)

        self.free_length = free_length

    def description(self):
        upto = ' up to' if self.free_length else ''
        desc = """\
    The '{plotly_name}' property is an info array that may be specified as a list or tuple of{upto}
    {N} elements such that:
        """.format(plotly_name=self.plotly_name, upto=upto, N=len(self.item_validators))

        for i, item_validator in enumerate(self.item_validators):
            el_desc = ('\n' + ' ' * 12).join([line.strip() for line in item_validator.description().split('\n')])
            desc = desc + """
      ({i}) {el_desc}
            """.format(i=i, el_desc=el_desc)

        return desc

    @staticmethod
    def build_validator(validator_info, plotly_name, parent_name):
        datatype = validator_info['valType']  # type: str
        validator_classname = datatype.title().replace('_', '') + 'Validator'
        validator_class = eval(validator_classname)

        kwargs = {k: validator_info[k] for k in validator_info
                  if k not in ['valType', 'description', 'role']}

        return validator_class(plotly_name=plotly_name, parent_name=parent_name, **kwargs)

    def validate_coerce(self, v):
        if v is None:
            # Pass None through
            pass
        elif not isinstance(v, (list, tuple)):
            self.raise_invalid_val(v)
        elif not self.free_length and len(v) != len(self.item_validators):
            self.raise_invalid_val(v)
        elif self.free_length and len(v) > len(self.item_validators):
            self.raise_invalid_val(v)
        else:
            # We have a list or tuple of the correct length
            v = list(v)
            for i, (el, validator) in enumerate(zip(v, self.item_validators)):
                # Validate coerce elements
                v[i] = validator.validate_coerce(el)

        return v


class ImageUriValidator(BaseValidator):
    _PIL = None

    try:
        _PIL = import_module('PIL')
    except ModuleNotFoundError:
        pass

    def __init__(self, plotly_name, parent_name, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

    def description(self):

        desc = """\
    The '{plotly_name}' property is an image URI that may be specified as:
      - A remote image URI string (e.g. 'http://www.somewhere.com/image.png')
      - A local image URI string (e.g. 'file:////somewhere/image.png')
      - A data URI image string (e.g. 'data:image/png;base64,iVBORw0KGgoAAAANSU')
      - A PIL.Image.Image object which will be immediately converted to a data URI image string
            See http://pillow.readthedocs.io/en/latest/reference/Image.html
        """.format(plotly_name=self.plotly_name)
        return desc

    def validate_coerce(self, v):
        if v is None:
            pass
        elif isinstance(v, str):
            # Future possibilities:
            #   - Detect filesystem system paths and convert to URI
            #   - Validate either url or data uri
            pass
        elif self._PIL and isinstance(v, self._PIL.Image.Image):
            # Convert PIL image to png data uri string
            in_mem_file = io.BytesIO()
            v.save(in_mem_file, format="PNG")
            in_mem_file.seek(0)
            img_bytes = in_mem_file.read()
            base64_encoded_result_bytes = base64.b64encode(img_bytes)
            base64_encoded_result_str = base64_encoded_result_bytes.decode('ascii')
            v = 'data:image/png;base64,{base64_encoded_result_str}'.format(
                base64_encoded_result_str=base64_encoded_result_str)
        else:
            self.raise_invalid_val(v)

        return v


class CompoundValidator(BaseValidator):
    def __init__(self, plotly_name, parent_name,
                 data_class_str, data_docs, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

        # Save element class string
        self.data_class_str = data_class_str
        self._data_class = None
        self.data_docs = data_docs
        self.module_str = CompoundValidator.compute_graph_obj_module_str(
            self.data_class_str, parent_name)

    @staticmethod
    def import_graph_objs_class(data_class_str, module_str):
        # Import class module
        module = import_module(module_str)

        # Get class reference
        return getattr(module, data_class_str)

    @staticmethod
    def compute_graph_obj_module_str(data_class_str, parent_name):
        if parent_name == 'frame' and data_class_str in ['Data', 'Layout']:
            # Special case. There are no graph_objs.frame.Data or
            # graph_objs.frame.Layout classes. These are remapped to
            # graph_objs.Data and graph_objs.Layout

            parent_parts = parent_name.split('.')
            module_str = '.'.join(['plotly.graph_objs'] + parent_parts[1:])
        elif parent_name:
            module_str = 'plotly.graph_objs.' + parent_name
        else:
            module_str = 'plotly.graph_objs'

        return  module_str

    @property
    def data_class(self):
        if self._data_class is None:
            self._data_class = CompoundValidator.import_graph_objs_class(
                self.data_class_str, self.module_str)

        return self._data_class

    def description(self):

        desc = ("""\
    The '{plotly_name}' property is an instance of {class_str} 
    that may be specified as:
      - An instance of {module_str}.{class_str}
      - A dict of string/value properties that will be passed to the 
        {class_str} constructor
      
        Supported dict properties:
            {constructor_params_str}"""
                ).format(plotly_name=self.plotly_name,
                         class_str=self.data_class_str,
                         module_str=self.module_str,
                         constructor_params_str=self.data_docs)

        return desc

    def validate_coerce(self, v):
        if isinstance(self.data_class, str):
            raise ValueError("Invalid data_class of type 'string': {data_class}"
                             .format(data_class = self.data_class))

        if v is None:
            v = self.data_class()

        elif isinstance(v, dict):
            v = self.data_class(**v)

        elif isinstance(v, self.data_class):
            # Copy object
            v = self.data_class(**v._props)
        else:
            self.raise_invalid_val(v)

        v._plotly_name = self.plotly_name
        return v


class CompoundArrayValidator(BaseValidator):
    def __init__(self, plotly_name, parent_name,
                 data_class_str, data_docs, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)

        # Save element class string
        self.data_class_str = data_class_str
        self._data_class = None

        self.data_docs = data_docs
        self.module_str = CompoundValidator.compute_graph_obj_module_str(
            self.data_class_str, parent_name)

    def description(self):

        desc = ("""\
    The '{plotly_name}' property is a tuple of instances of {class_str} that may be specified as:
      - A list or tuple of instances of {module_str}.{class_str}
      - A list or tuple of dicts of string/value properties that will be passed to the {class_str} constructor

        Supported dict properties:
            {constructor_params_str}"""
                ).format(plotly_name=self.plotly_name,
                         class_str=self.data_class_str,
                         module_str=self.module_str,
                         constructor_params_str=self.data_docs)

        return desc

    @property
    def data_class(self):
        if self._data_class is None:
            self._data_class = CompoundValidator.import_graph_objs_class(
                self.data_class_str, self.module_str)

        return self._data_class

    def validate_coerce(self, v):

        if isinstance(self.data_class, str):
            raise ValueError("Invalid data_class of type 'string': {data_class}"
                             .format(data_class=self.data_class))

        if v is None:
            v = ()

        elif isinstance(v, (list, tuple)):
            res = []
            invalid_els = []
            for v_el in v:
                if isinstance(v_el, self.data_class):
                    res.append(v_el)
                elif isinstance(v_el, dict):
                    res.append(self.data_class(**v_el))
                else:
                    res.append(None)
                    invalid_els.append(v_el)

            if invalid_els:
                self.raise_invalid_elements(invalid_els)

            v = tuple(res)

        elif not isinstance(v, str):
            self.raise_invalid_val(v)

        return v


class BaseDataValidator(BaseValidator):
    def __init__(self, class_map, plotly_name, parent_name, **kwargs):
        super().__init__(plotly_name=plotly_name,
                         parent_name=parent_name, **kwargs)
        self.class_strs_map = class_map
        self._class_map = None

    def description(self):

        trace_types = str(list(self.class_strs_map.keys()))

        trace_types_wrapped = '\n'.join(textwrap.wrap(trace_types,
                                                      subsequent_indent=' ' * 21,
                                                      width=79 - 8))

        desc = ("""\
    The '{plotly_name}' property is a tuple of trace instances that may be specified as:
      - A list or tuple of trace instances 
        (e.g. [Scatter(...), Bar(...)])
      - A list or tuple of dicts of string/value properties where:
        - The 'type' property specifies the trace type
            One of: {trace_types}
                     
        - All remaining properties are passed to the constructor of the specified trace type

        (e.g. [{{'type': 'scatter', ...}}, {{'type': 'bar, ...}}])"""
                ).format(plotly_name=self.plotly_name, trace_types=trace_types_wrapped)

        return desc

    @property
    def class_map(self):
        if self._class_map is None:

            # Initialize class map
            self._class_map = {}

            # Import trace classes
            trace_module = import_module('plotly.graph_objs')
            for k, class_str in self.class_strs_map.items():
                self._class_map[k] = getattr(trace_module, class_str)

        return self._class_map

    def validate_coerce(self, v):

        if v is None:
            v = ()
        elif isinstance(v, (list, tuple)):
            trace_classes = tuple(self.class_map.values())

            res = []
            invalid_els = []
            for v_el in v:

                if isinstance(v_el, trace_classes):
                    # Clone input traces
                    v_el = v_el.to_plotly_json()

                if isinstance(v_el, dict):
                    v_copy = deepcopy(v_el)

                    if 'type' in v_copy:
                        trace_type = v_copy.pop('type')
                    else:
                        trace_type = 'scatter'

                    if trace_type not in self.class_map:
                        res.append(None)
                        invalid_els.append(v_el)
                    else:
                        trace = self.class_map[trace_type](**v_copy)
                        res.append(trace)
                else:
                    res.append(None)
                    invalid_els.append(v_el)

            if invalid_els:
                self.raise_invalid_elements(invalid_els)

            v = tuple(res)

            # Set new UIDs
            for trace in v:
                trace.uid = str(uuid.uuid1())

        else:
            self.raise_invalid_val(v)

        return v
