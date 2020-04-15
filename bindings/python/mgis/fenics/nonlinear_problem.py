#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MFrontNonlinearProblem and MFrontOptimisation classes

@author: Jeremy Bleyer, Ecole des Ponts ParisTech,
Laboratoire Navier (ENPC,IFSTTAR,CNRS UMR 8205)
@email: jeremy.bleyer@enpc.f
"""
from dolfin import *
from .utils import *
from .gradient_flux import *
import mgis.behaviour as mgis_bv
from ufl import shape

SymGrad = {mgis_bv.Hypothesis.Tridimensional: lambda u: sym(grad(u)),
           mgis_bv.Hypothesis.PlaneStrain: lambda u: sym(grad(u)),
           mgis_bv.Hypothesis.Axisymmetrical: lambda r, u: sym(axi_grad(r, u))}
Grad =  {mgis_bv.Hypothesis.Tridimensional: lambda u: grad(u),
         mgis_bv.Hypothesis.PlaneStrain: lambda u: grad(u),
         mgis_bv.Hypothesis.Axisymmetrical: lambda r, u: axi_grad(r, u)}
Id_Grad = {mgis_bv.Hypothesis.Tridimensional: lambda u: Identity(3)+grad(u),
           mgis_bv.Hypothesis.PlaneStrain: lambda u: Identity(2)+grad(u),
           mgis_bv.Hypothesis.Axisymmetrical: lambda r, u: Identity(3)+axi_grad(r, u)}

predefined_gradients = {
    "Strain": SymGrad,
    "TemperatureGradient": Grad,
    "DeformationGradient": Id_Grad}

predefined_external_state_variables = {"Temperature": Constant(293.15)}

def list_predefined_gradients():
    print("'TFEL gradient name'   (Available hypotheses)")
    print("---------------------------------------------")
    for (g, value) in predefined_gradients.items():
        print("{:22} {}".format("'{}'".format(g), tuple(str(v) for v in value.keys())))
    print("")

class AbstractNonlinearProblem:
    """
    This class handles the definition and resolution of a nonlinear problem associated with a `MFrontNonlinearMaterial`.
    """
    def __init__(self, u, material, quadrature_degree=2, bcs=None):
        self.u = u
        self.V = self.u.function_space()
        self.u_ = TestFunction(self.V)
        self.du = TrialFunction(self.V)
        self.mesh = self.V.mesh()
        self.material = material
        self.axisymmetric = self.material.hypothesis==mgis_bv.Hypothesis.Axisymmetrical
        self.integration_type = mgis_bv.IntegrationType.IntegrationWithConsistentTangentOperator

        self.quadrature_degree = quadrature_degree


        cell = self.mesh.ufl_cell()
        W0e = get_quadrature_element(cell, self.quadrature_degree)
        # scalar quadrature space
        self.W0 = FunctionSpace(self.mesh, W0e)
        # compute Gauss points numbers
        self.ngauss = Function(self.W0).vector().local_size()
        # Set data manager
        self.material.set_data_manager(self.ngauss)
        # dummy function used for computing global quantity
        self._dummy_function = Function(self.W0)

        if self.material.hypothesis == mgis_bv.Hypothesis.Tridimensional:
            assert self.u.geometric_dimension()==3, "Conflicting geometric dimension and material hypothesis"
        else:
            assert self.u.geometric_dimension()==2, "Conflicting geometric dimension and material hypothesis"

        if bcs is None:
            self.bcs = []
        else:
            self.bcs = bcs
        self.dx = Measure("dx", metadata={"quadrature_degree": self.quadrature_degree,
                                          "quadrature_scheme": "default"})
        if self.axisymmetric:
            x = SpatialCoordinate(self.mesh)
            self._r = x[0]
            self.axi_coeff = 2*pi*abs(x[0])
        else:
            self.axi_coeff = 1

        self._Fext = None
        self._init = True

        self.state_variables =  {"internal": dict.fromkeys(self.material.get_internal_state_variable_names(), None),
                                 "external": dict.fromkeys(self.material.get_external_state_variable_names(), None)}
        self.gradients = dict.fromkeys(self.material.get_gradient_names(), None)
        self.fluxes = {}

        self.initialize_internal_state_variables()

    def automatic_registration(self):
        """
        Performs automatic registration of predefined Gradients or external state variables.
        """
        for (name, value) in self.gradients.items():
            if value is None:
                if self.axisymmetric:
                    var = (self._r, self.u)
                else:
                    var = (self.u,)
                case = predefined_gradients.get(name, None)
                if case is not None:
                    expr = case[self.material.hypothesis](*var)
                    self.register_gradient(name, expr)
                    print("Automatic registration of '{}' as {}.\n".format(name, str(expr)))
        for (name, value) in self.state_variables["external"].items():
            if name in predefined_external_state_variables and value is None:
                if self.u.name() == name:
                    self.register_external_state_variable(name, self.u)
                    print("Automatic registration of '{}' as an external state variable.\n".format(name))
                elif predefined_external_state_variables.get(name, None):
                    val = predefined_external_state_variables[name]
                    self.register_external_state_variable(name, val)
                    print("Automatic registration of '{}' as a Constant value = {}.\n".format(name, float(val)))


    def define_form(self):
        # residual form (internal forces)
        self.residual = sum([inner(g.variation(self.u_), f.function)
                          for (f, g) in zip(self.fluxes.values(), self.gradients.values())])*self.axi_coeff*self.dx
        if self._Fext is not None:
            self.residual -= self._Fext
        self.compute_tangent_form()

    def compute_tangent_form(self):
        # tangent bilinear form
        self.tangent_a = sum([derivative(self.residual, f.function, t*dg.variation(self.du))
                      for f in self.fluxes.values()  for (t, dg) in zip(f.tangent_blocks.values(), f.gradients)])


    def register_gradient(self, name, expression):
        """
        Register a MFront gradient with a UFL expression.

        Parameters
        ----------
        name : str
            Name of the MFront gradient to be registered.
        expression : UFL expression
            UFL expression corresponding to the gradient
        """
        pos = self.material.get_gradient_names().index(name)
        size = self.material.get_gradient_sizes()[pos]
        vtype = self.material.behaviour.gradients[pos].type
        if vtype == mgis_bv.VariableType.Tensor:
            symmetric = False
        elif vtype == mgis_bv.VariableType.Stensor:
            symmetric = True
        else:
            symmetric = None
        self.gradients.update({name: Gradient(self.u, expression, name, symmetric=symmetric)})

    def register_external_state_variable(self, name, expression):
        """
        Register a MFront external state variable with a UFL expression.

        Parameters
        ----------
        name : str
            Name of the MFront external state variable to be registered.
        expression : UFL expression, `dolfin.Constant`, float
            Expression corresponding to the external state variable
        """
        pos = self.material.get_external_state_variable_names().index(name)
        size = self.material.get_external_state_variable_sizes()[pos]
        vtype = self.material.behaviour.external_state_variables[pos].type
        if vtype != mgis_bv.VariableType.Scalar:
            raise NotImplementedError("Only scalar external state variables are handled")
        if isinstance(expression, Constant):
            self.state_variables["external"].update({name: expression})
        elif type(expression) == float:
            self.state_variables["external"].update({name: Constant(expression)})
        else:
            self.state_variables["external"].update({name: Var(expression, name)})

    def set_loading(self, Fext):
        """
        Adds external forces to residual form

        Parameters
        ----------
        Fext : UFL expression
            Action :math:`L(u)` of the external force linear form on the mechanical field
        """
        self._Fext = ufl.replace(Fext, {self.u: self.u_})

    def set_quadrature_function_spaces(self):
        cell = self.mesh.ufl_cell()
        W0e = get_quadrature_element(cell, self.quadrature_degree)
        # scalar quadrature space
        self.W0 = FunctionSpace(self.mesh, W0e)
        # compute Gauss points numbers
        self.ngauss = Function(self.W0).vector().local_size()
        # Set data manager
        self.material.set_data_manager(self.ngauss)

        # Get strain measure dimension
        self.strain_dim = ufl.shape(self.strain_measure(self.u))[0]
        # Define quadrature spaces for stress/strain and tangent matrix
        Wsige = get_quadrature_element(cell, self.quadrature_degree, self.strain_dim)
        # stress/strain quadrature space
        self.Wsig = FunctionSpace(self.mesh, Wsige)
        Wce = get_quadrature_element(cell, self.quadrature_degree, (self.strain_dim, self.strain_dim))
        # tangent matrix quadrature space
        self.WCt = FunctionSpace(self.mesh, Wce)

    def initialize_internal_state_variables(self):
        for (s, size) in zip(self.material.get_internal_state_variable_names(), self.material.get_internal_state_variable_sizes()):
            We = get_quadrature_element(self.mesh.ufl_cell(), self.quadrature_degree, size)
            W = FunctionSpace(self.mesh, We)
            self.state_variables["internal"][s] = Function(W, name=s)

    def initialize_fields(self):
        """
        Initializes Quadrature functions associated with Gradient/Flux objects
        and the corresponding tangent blocks.

        All gradients and external state variables must have been registered first.

        Automatic registration is performed at the beginning of the method call.

        This method has to be called once before calling `solve`.
        """
        self.automatic_registration()

        for (s, size) in zip(self.material.get_external_state_variable_names(), self.material.get_external_state_variable_sizes()):
            state_var = self.state_variables["external"][s]
            if isinstance(state_var, Gradient):
                state_var.initialize_function(self.mesh, self.quadrature_degree)
            elif isinstance(state_var, Constant):
                pass
            else:
                raise ValueError("External state variable '{}' has not been registered.".format(s))

        for (g, size) in zip(self.material.get_gradient_names(), self.material.get_gradient_sizes()):
            try:
                gradient = self.gradients[g].initialize_function(self.mesh, self.quadrature_degree)
            except:
                raise ValueError("Gradient '{}' has not been registered.".format(g))

        for (f, size) in zip(self.material.get_flux_names(), self.material.get_flux_sizes()):
            flux_gradients = []
            for t in self.material.get_tangent_block_names():
                if t[0]==f:
                    try:
                        flux_gradients.append(self.gradients[t[1]])
                    except:
                        value = self.state_variables["external"].get(t[1], None)
                        if value is not None and isinstance(value, Var):
                                flux_gradients.append(value)
                        else:
                            raise ValueError("'{}' could not be associated with a registered gradient or state variable.".format(t[1]))
            flux = Flux(f, size, flux_gradients)
            flux.initialize_functions(self.mesh, self.quadrature_degree)
            self.fluxes.update({f: flux})

        self.define_form()

        self.material.update_external_state_variables(self.state_variables["external"])
        mgis_bv.integrate(self.material.data_manager,
                          self.integration_type, 0, 0, self.material.data_manager.n)
        self.affect_gradients()
        self.block_names = self.material.get_tangent_block_names()
        self.block_shapes = self.material.get_tangent_block_sizes()
        self.flattened_block_shapes = [s[0]*s[1] if len(s)==2 else s[0] for s in self.block_shapes]
        self.update_tangent_blocks()

        self._init = True

    def update_tangent_blocks(self):
        buff = 0
        for (i, block) in enumerate(self.block_names):
            f, g = block
            try:
                t = self.fluxes[f].tangent_blocks[g]
            except KeyError:
                try:
                    t = self.state_variables["internal"][f]
                except KeyError:
                    raise KeyError("'{}' could not be found as a flux or an internal state variable.")
            block_shape = self.flattened_block_shapes[i]
            t.vector().set_local(self.material.data_manager.K[:,buff:buff+block_shape].flatten())
            buff += block_shape

    def update_fluxes(self):
        buff = 0
        for (i, f) in enumerate(self.material.get_flux_names()):
            flux = self.fluxes[f]
            block_shape = self.material.get_flux_sizes()[i]
            flux.function.vector().set_local(self.material.data_manager.s1.thermodynamic_forces[:,buff:buff+block_shape].flatten())
            buff += block_shape

    def update_gradients(self):
        buff = 0
        for (i, g) in enumerate(self.material.get_gradient_names()):
            gradient = self.gradients[g]
            gradient.update()
            block_shape = self.material.get_gradient_sizes()[i]
            grad_vals = gradient.function.vector().get_local()
            if gradient.shape > 0:
                grad_vals = grad_vals.reshape((self.material.data_manager.n, gradient.shape))
            else:
                grad_vals = grad_vals[:, np.newaxis]
            self.material.data_manager.s1.gradients[:, buff:buff+block_shape] = grad_vals
            buff += block_shape

    def affect_gradients(self):
        buff = 0
        for (i, g) in enumerate(self.material.get_gradient_names()):
            gradient = self.gradients[g]
            gradient.update()
            block_shape = self.material.get_gradient_sizes()[i]
            grad_vals = gradient.function.vector().get_local()
            if gradient.shape > 0:
                grad_vals = grad_vals.reshape((self.material.data_manager.n, gradient.shape))
            else:
                grad_vals = grad_vals[:, np.newaxis]
            self.material.data_manager.s0.gradients[:, buff:buff+block_shape] = grad_vals
            buff += block_shape

    def affect_internal_state_variables(self):
        buff = 0
        for (i, s) in enumerate(self.material.get_internal_state_variable_names()):
            state_var = self.state_variables["internal"][s]
            if state_var is None:
                raise ValueError("The state variable '{}' has not been initialized.".format(s))
            block_shape = self.material.get_internal_state_variable_sizes()[i]
            self.material.data_manager.s0.internal_state_variables[:,buff:buff+block_shape] = state_var.vector().get_local().reshape(self.material.data_manager.n, block_shape)
            buff += block_shape

    def update_internal_state_variables(self):
        """Performs update of internal state variables"""
        buff = 0
        for (i, s) in enumerate(self.material.get_internal_state_variable_names()):
            state_var = self.state_variables["internal"][s]
            block_shape = self.material.get_internal_state_variable_sizes()[i]
            state_var.vector().set_local(self.material.data_manager.s1.internal_state_variables[:,buff:buff+block_shape].flatten())
            buff += block_shape

    def update_constitutive_law(self, u):
        """Performs the consitutive law update"""
        self.update_gradients()
        self.material.update_external_state_variables(self.state_variables["external"])
        # integrate the behaviour
        mgis_bv.integrate(self.material.data_manager, self.integration_type,
                          0, 0, self.material.data_manager.n);
        # getting the stress and consistent tangent operator back to
        # the FEniCS world.
        self.update_fluxes()
        self.update_tangent_blocks()
        self.update_internal_state_variables()

    def get_state_variable(self, name):
        """
        Returns the function associated with an internal state variable

        Parameters
        ----------
        name : str
            Name of the internal state variable

        Returns
        -------
        `dolfin.Function`
            A function defined on the corresponding Quadrature function space

        """
        if self.state_variables["internal"][name] is None:
            position = self.material.get_internal_state_variable_names().index(name)
            shape = self.material.get_internal_state_variable_sizes()[position]
            We = get_quadrature_element(self.mesh.ufl_cell(), self.quadrature_degree, shape)
            W = FunctionSpace(self.mesh, We)
            self.state_variables["internal"][name] = Function(W, name=name)
        return self.state_variables["internal"][name]

    def get_previous_state_variable(self, name):
        s0 = self.state_variables["internal"].get(name + " (previous)", None)
        if s0 is None:
            s = self.state_variables["internal"][name]
            s0 = s.copy()
            print(s0.name())
            s0.rename(s.name() + " (previous)", s.name() + " (previous)")
            print(s0.name())
        return s0

    def get_flux(self, name):
        """Returns the Quadrature function associated with a flux"""
        return self.fluxes[name].function

    def get_previous_flux(self, name):
        f = self.fluxes[name].function
        f0 = f.copy()
        self.fluxes[name].previous = f0
        f0.rename(f.name() + " (previous)", f.name() + " (previous)")
        return f0

    def get_dissipated_energy(self):
        """Dissipated energy computed from MFront @DissipatedEnergy"""
        self._dummy_function.vector().set_local(self.material.data_manager.s1.dissipated_energies)
        return assemble(self._dummy_function*self.dx)

    def get_stored_energy(self):
        """Stored energy computed from MFront @InternalEnergy"""
        self._dummy_function.vector().set_local(self.material.data_manager.s1.stored_energies)
        return assemble(self._dummy_function*self.dx)

    def get_total_energy(self):
        """Total (stored + dissipated) energy"""
        return self.get_stored_energy()+self.get_dissipated_energy()

class MFrontNonlinearProblem(NonlinearProblem, AbstractNonlinearProblem):
    """
    This class handles the definition and resolution of a nonlinear problem
    associated with a `MFrontNonlinearMaterial`.
    """
    def __init__(self, u, material, quadrature_degree=2, bcs=None):
        """
        Parameters
        ----------
        u : `dolfin.Function`
            the unknown function
        material : `MFrontNonlinearMaterial`
            the nonlinear material instance associated with the project
        quadrature_degree : `int`, optional
            the quadrature degree used for performing the constitutive update integration. The default is 2.
        bcs : (list of) `dolfin.DirichletBC`, optional
            Dirichlet boundary conditions associated with the problem. The default is None.
        """
        NonlinearProblem.__init__(self)
        AbstractNonlinearProblem.__init__(self, u, material, quadrature_degree=quadrature_degree, bcs=bcs)
        self.solver = NewtonSolver()

    def form(self, A, P, b, x):
        # this function is called before calling F or J
        self.update_constitutive_law(self.u)
        assemble_system(self.tangent_a, self.residual, A_tensor=A, b_tensor=b, bcs=self.bcs, x0=x)

    def F(self,b,x):
        pass

    def J(self,A,x):
        pass

    def solve(self, x):
        if self._init:
            self.initialize_fields()
        solv_out = self.solver.solve(self, x)
        mgis_bv.update(self.material.data_manager)
        return solv_out

class MFrontOptimisationProblem(OptimisationProblem, AbstractNonlinearProblem):
    """
    This class handles the definition and resolution of a nonlinear optimization problem
    associated with a `MFrontNonlinearMaterial`.
    """
    def __init__(self, u, material, quadrature_degree=2, bcs=None):
        """
        Parameters
        ----------
        u : `dolfin.Function`
            the unknown function
        material : `MFrontNonlinearMaterial`
            the nonlinear material instance associated with the project
        quadrature_degree : `int`, optional
            the quadrature degree used for performing the constitutive update integration. The default is 2.
        bcs : (list of) `dolfin.DirichletBC`, optional
            Dirichlet boundary conditions associated with the problem. The default is None.
        """
        OptimisationProblem.__init__(self)
        AbstractNonlinearProblem.__init__(self, u, material, quadrature_degree=quadrature_degree, bcs=bcs)
        self.solver = PETScTAOSolver()
        self.solver.parameters["method"] = "tron"
        self.solver.parameters["line_search"] = "gpcg"


    def form(self, A, P, b, x):
        pass

    def f(self, x):
        """Objective function"""
        self.u.vector()[:] = x
        self.update_constitutive_law(self.u)
        mgis_bv.update(self.material.data_manager)
        return self.get_total_energy()

    def F(self, b, x):
        """Objective function gradient"""
        self.u.vector()[:] = x
        assemble(self.residual, b)
        if type(self.bcs) == list:
            for bc in self.bcs:
                bc.apply(b)
        else:
            self.bcs.apply(b)

    def J(self, A, x):
        """Objective function Hessian"""
        self.u.vector()[:] = x
        assemble(self.tangent_a, A)
        if type(self.bcs) == list:
            for bc in self.bcs:
                bc.apply(A)
        else:
            self.bcs.apply(A)

    def solve(self, x, lx, ux):
        """
        Solves the optimization problem

        .. math:: \\min_{lx \\leq x \\leq ux} f(x)

        Parameters
        ----------
        x : vector
            optimization variable
        lx : vector
            lower bound on variable
        ux : vector
            upper bound on variable

        """
        if self._init:
            self.initialize_fields()
        self.solver.solve(self, x, lx, ux)