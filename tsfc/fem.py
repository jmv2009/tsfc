from __future__ import absolute_import

import collections
import itertools

import numpy
from singledispatch import singledispatch

from ufl.corealg.map_dag import map_expr_dag, map_expr_dags
from ufl.corealg.multifunction import MultiFunction
from ufl.classes import (Argument, Coefficient, FormArgument,
                         GeometricQuantity, QuadratureWeight)

from gem import gem

from tsfc.constants import PRECISION
from tsfc.fiatinterface import create_element, as_fiat_cell
from tsfc.modified_terminals import analyse_modified_terminal
from tsfc import compat
from tsfc import ufl2gem
from tsfc import geometric
from tsfc.ufl_utils import (CollectModifiedTerminals,
                            ModifiedTerminalMixin, PickRestriction,
                            spanning_degree, simplify_abs)


# FFC uses one less digits for rounding than for printing
epsilon = eval("1e-%d" % (PRECISION - 1))


def _tabulate(ufl_element, order, points):
    """Ask FIAT to tabulate ``points`` up to order ``order``, then
    rearranges the result into a series of ``(c, D, table)`` tuples,
    where:

    c: component index (for vector-valued and tensor-valued elements)
    D: derivative tuple (e.g. (1, 2) means d/dx d^2/dy^2)
    table: tabulation matrix for the given component and derivative.
           shape: len(points) x space_dimension

    :arg ufl_element: element to tabulate
    :arg order: FIAT gives all derivatives up to this order
    :arg points: points to tabulate the element on
    """
    element = create_element(ufl_element)
    phi = element.space_dimension()
    C = ufl_element.reference_value_size() - len(ufl_element.symmetry())
    q = len(points)
    for D, fiat_table in element.tabulate(order, points).iteritems():
        reordered_table = fiat_table.reshape(phi, C, q).transpose(1, 2, 0)  # (C, q, phi)
        for c, table in enumerate(reordered_table):
            yield c, D, table


def tabulate(ufl_element, order, points):
    """Same as the above, but also applies FFC rounding and recognises
    cellwise constantness.  Cellwise constantness is determined
    symbolically, but we also check the numerics to be safe."""
    for c, D, table in _tabulate(ufl_element, order, points):
        # Copied from FFC (ffc/quadrature/quadratureutils.py)
        table[abs(table) < epsilon] = 0
        table[abs(table - 1.0) < epsilon] = 1.0
        table[abs(table + 1.0) < epsilon] = -1.0
        table[abs(table - 0.5) < epsilon] = 0.5
        table[abs(table + 0.5) < epsilon] = -0.5

        if spanning_degree(ufl_element) <= sum(D):
            assert compat.allclose(table, table.mean(axis=0, keepdims=True), equal_nan=True)
            table = table[0]

        yield c, D, table


def make_tabulator(points):
    """Creates a tabulator for an array of points."""
    return lambda elem, order: tabulate(elem, order, points)


class TabulationManager(object):
    """Manages the generation of tabulation matrices for the different
    integral types."""

    def __init__(self, integral_type, cell, points):
        """Constructs a TabulationManager.

        :arg integral_type: integral type
        :arg cell: UFL cell
        :arg points: points on the integration entity (e.g. points on
                     an interval for facet integrals on a triangle)
        """
        self.integral_type = integral_type
        self.points = points

        self.tabulators = []
        self.tables = {}

        if integral_type == 'cell':
            self.tabulators.append(make_tabulator(points))

        elif integral_type in ['exterior_facet', 'interior_facet']:
            for entity in range(cell.num_facets()):
                t = as_fiat_cell(cell).get_facet_transform(entity)
                self.tabulators.append(make_tabulator(numpy.asarray(map(t, points))))

        elif integral_type in ['exterior_facet_bottom', 'exterior_facet_top', 'interior_facet_horiz']:
            for entity in range(2):  # top and bottom
                t = as_fiat_cell(cell).get_horiz_facet_transform(entity)
                self.tabulators.append(make_tabulator(numpy.asarray(map(t, points))))

        elif integral_type in ['exterior_facet_vert', 'interior_facet_vert']:
            for entity in range(cell.sub_cells()[0].num_facets()):  # "base cell" facets
                t = as_fiat_cell(cell).get_vert_facet_transform(entity)
                self.tabulators.append(make_tabulator(numpy.asarray(map(t, points))))

        else:
            raise NotImplementedError("integral type %s not supported" % integral_type)

    def tabulate(self, ufl_element, max_deriv):
        """Prepare the tabulations of a finite element up to a given
        derivative order.

        :arg ufl_element: UFL element to tabulate
        :arg max_deriv: tabulate derivatives up this order
        """
        store = collections.defaultdict(list)
        for tabulator in self.tabulators:
            for c, D, table in tabulator(ufl_element, max_deriv):
                store[(ufl_element, c, D)].append(table)

        if self.integral_type == 'cell':
            for key, (table,) in store.iteritems():
                self.tables[key] = table
        else:
            for key, tables in store.iteritems():
                table = numpy.array(tables)
                if len(table.shape) == 2:
                    # Cellwise constant; must not depend on the facet
                    assert compat.allclose(table, table.mean(axis=0, keepdims=True), equal_nan=True)
                    table = table[0]
                self.tables[key] = table

    def __getitem__(self, key):
        return self.tables[key]


class Translator(MultiFunction, ModifiedTerminalMixin, ufl2gem.Mixin):
    """Contains all the context necessary to translate UFL into GEM."""

    def __init__(self, tabulation_manager, weights, quadrature_index,
                 argument_indices, coefficient_mapper, index_cache):
        MultiFunction.__init__(self)
        ufl2gem.Mixin.__init__(self)
        integral_type = tabulation_manager.integral_type
        self.integral_type = integral_type
        self.tabulation_manager = tabulation_manager
        self.weights = gem.Literal(weights)
        self.quadrature_index = quadrature_index
        self.argument_indices = argument_indices
        self.coefficient_mapper = coefficient_mapper
        self.index_cache = index_cache

        if integral_type in ['exterior_facet', 'exterior_facet_vert']:
            self.facet = {None: gem.VariableIndex(gem.Indexed(gem.Variable('facet', (1,)), (0,)))}
        elif integral_type in ['interior_facet', 'interior_facet_vert']:
            self.facet = {'+': gem.VariableIndex(gem.Indexed(gem.Variable('facet', (2,)), (0,))),
                          '-': gem.VariableIndex(gem.Indexed(gem.Variable('facet', (2,)), (1,)))}
        elif integral_type == 'exterior_facet_bottom':
            self.facet = {None: 0}
        elif integral_type == 'exterior_facet_top':
            self.facet = {None: 1}
        elif integral_type == 'interior_facet_horiz':
            self.facet = {'+': 1, '-': 0}
        else:
            self.facet = None

        if self.integral_type.startswith("interior_facet"):
            self.cell_orientations = gem.Variable("cell_orientations", (2, 1))
        else:
            self.cell_orientations = gem.Variable("cell_orientations", (1, 1))

    def select_facet(self, tensor, restriction):
        """Applies facet selection on a GEM tensor if necessary.

        :arg tensor: GEM tensor
        :arg restriction: restriction on the modified terminal
        :returns: another GEM tensor
        """
        if self.integral_type == 'cell':
            return tensor
        else:
            f = self.facet[restriction]
            return gem.partial_indexed(tensor, (f,))

    def modified_terminal(self, o):
        """Overrides the modified terminal handler from
        :class:`ModifiedTerminalMixin`."""
        mt = analyse_modified_terminal(o)
        return translate(mt.terminal, mt, self)


def iterate_shape(mt, callback):
    """Iterates through the components of a modified terminal, and
    calls ``callback`` with ``(ufl_element, c, D)`` keys which are
    used to look up tabulation matrix for that component.  Then
    assembles the result into a GEM tensor (if tensor-valued)
    corresponding to the modified terminal.

    :arg mt: analysed modified terminal
    :arg callback: callback to get the GEM translation of a component
    :returns: GEM translation of the modified terminal

    This is a helper for translating Arguments and Coefficients.
    """
    ufl_element = mt.terminal.ufl_element()
    dim = ufl_element.cell().topological_dimension()

    def flat_index(ordered_deriv):
        return tuple((numpy.asarray(ordered_deriv) == d).sum() for d in range(dim))

    ordered_derivs = itertools.product(range(dim), repeat=mt.local_derivatives)
    flat_derivs = map(flat_index, ordered_derivs)

    result = []
    for c in range(ufl_element.reference_value_size()):
        for flat_deriv in flat_derivs:
            result.append(callback((ufl_element, c, flat_deriv)))

    shape = mt.expr.ufl_shape
    assert len(result) == numpy.prod(shape)

    if shape:
        return gem.ListTensor(numpy.asarray(result).reshape(shape))
    else:
        return result[0]


@singledispatch
def translate(terminal, mt, params):
    """Translates modified terminals into GEM.

    :arg terminal: terminal, for dispatching
    :arg mt: analysed modified terminal
    :arg params: translator context
    :returns: GEM translation of the modified terminal
    """
    raise AssertionError("Cannot handle terminal type: %s" % type(terminal))


@translate.register(QuadratureWeight)  # noqa: Not actually redefinition
def _(terminal, mt, params):
    return gem.Indexed(params.weights, (params.quadrature_index,))


@translate.register(GeometricQuantity)  # noqa: Not actually redefinition
def _(terminal, mt, params):
    return geometric.translate(terminal, mt, params)


@translate.register(Argument)  # noqa: Not actually redefinition
def _(terminal, mt, params):
    argument_index = params.argument_indices[terminal.number()]

    def callback(key):
        table = params.tabulation_manager[key]
        if len(table.shape) == 1:
            # Cellwise constant
            row = gem.Literal(table)
        else:
            table = params.select_facet(gem.Literal(table), mt.restriction)
            row = gem.partial_indexed(table, (params.quadrature_index,))
        return gem.Indexed(row, (argument_index,))

    return iterate_shape(mt, callback)


@translate.register(Coefficient)  # noqa: Not actually redefinition
def _(terminal, mt, params):
    kernel_arg = params.coefficient_mapper(terminal)

    if terminal.ufl_element().family() == 'Real':
        assert mt.local_derivatives == 0
        return kernel_arg

    ka = gem.partial_indexed(kernel_arg, {None: (), '+': (0,), '-': (1,)}[mt.restriction])

    def callback(key):
        table = params.tabulation_manager[key]
        if len(table.shape) == 1:
            # Cellwise constant
            row = gem.Literal(table)
            if numpy.count_nonzero(table) <= 2:
                assert row.shape == ka.shape
                return reduce(gem.Sum,
                              [gem.Product(gem.Indexed(row, (i,)), gem.Indexed(ka, (i,)))
                               for i in range(row.shape[0])],
                              gem.Zero())
        else:
            table = params.select_facet(gem.Literal(table), mt.restriction)
            row = gem.partial_indexed(table, (params.quadrature_index,))

        r = params.index_cache[terminal.ufl_element()]
        return gem.IndexSum(gem.Product(gem.Indexed(row, (r,)),
                                        gem.Indexed(ka, (r,))), r)

    return iterate_shape(mt, callback)


def process(integral_type, cell, points, weights, quadrature_index,
            argument_indices, integrand, coefficient_mapper, index_cache):
    # Abs-simplification
    integrand = simplify_abs(integrand)

    # Collect modified terminals
    modified_terminals = []
    map_expr_dag(CollectModifiedTerminals(modified_terminals), integrand)

    # Collect maximal derivatives that needs tabulation
    max_derivs = collections.defaultdict(int)

    for mt in map(analyse_modified_terminal, modified_terminals):
        if isinstance(mt.terminal, FormArgument):
            ufl_element = mt.terminal.ufl_element()
            max_derivs[ufl_element] = max(mt.local_derivatives, max_derivs[ufl_element])

    # Collect tabulations for all components and derivatives
    tabulation_manager = TabulationManager(integral_type, cell, points)
    for ufl_element, max_deriv in max_derivs.items():
        if ufl_element.family() != 'Real':
            tabulation_manager.tabulate(ufl_element, max_deriv)

    if integral_type.startswith("interior_facet"):
        expressions = []
        for rs in itertools.product(("+", "-"), repeat=len(argument_indices)):
            expressions.append(map_expr_dag(PickRestriction(*rs), integrand))
    else:
        expressions = [integrand]

    # Translate UFL to Einstein's notation,
    # lowering finite element specific nodes
    translator = Translator(tabulation_manager, weights,
                            quadrature_index, argument_indices,
                            coefficient_mapper, index_cache)
    return map_expr_dags(translator, expressions)
