from collections import OrderedDict
import numpy as np
import torch
import warnings
from torch import nn as nn
from torch.nn import Parameter
from torch.nn import functional as F
from torch.nn import ModuleDict


class Readout():
    def initialize(self, *args, **kwargs):
        raise NotImplementedError('initialize is not implemented for ', self.__class__.__name__)

    def __repr__(self):
        s = super().__repr__()
        s += ' [{} regularizers: '.format(self.__class__.__name__)
        ret = []
        for attr in filter(lambda x: not x.startswith('_') and
                                     ('gamma' in x or 'pool' in x or 'positive' in x), dir(self)):
            ret.append('{} = {}'.format(attr, getattr(self, attr)))
        return s + '|'.join(ret) + ']\n'


#################
# Gaussian Readout
################

class MultipleGaussian2d(Readout, ModuleDict):
    """

    "MultipleGaussian2d" instantiates multiple instances of Gaussian2d Readouts
    usually used when dealing with different datasets or areas sharing the same core.
    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        loaders (list):  a list of dataset objects
        gamma_readout (float): regularisation term for the readout
    """

    def __init__(self, in_shape, loaders, gamma_readout, **kwargs):
        super().__init__()

        self.in_shape = in_shape
        self.neurons = OrderedDict([(k, loader.dataset.n_neurons) for k, loader in loaders.items()])

        self.gamma_readout = gamma_readout

        for k, n_neurons in self.neurons.items():
            self.add_module(k, Gaussian2d(in_shape=in_shape, outdims=n_neurons, **kwargs))

    def initialize(self, mean_activity_dict):

        for k, mu in mean_activity_dict.items():
            self[k].initialize()
            self[k].bias.data = mu.squeeze() - 1

    def regularizer(self, readout_key):
        return self[readout_key].feature_l1() * self.gamma_readout


class Gaussian2d(nn.Module):
    """

    'Gaussian2d' class instantiates an object that can used to learn a point in the core feature space for each neuron,
    sampled from a Gaussian distribution with some mean and variance at train but set to mean at test time, that best predicts its response.

    The readout receives the shape of the core as 'in_shape', the number of units/neurons being predicted as 'outdims', 'bias' specifying whether
    or not bias term is to be used and 'init_range' range for initialising the mean and variance of the gaussian distribution from which we sample to
    uniform distribution, U(-init_mu_range,init_mu_range) and  uniform distribution, U(0.0, init_sigma_range) respectively.
    The grid parameter contains the normalized locations (x, y coordinates in the core feature space) and is clipped to [-1.1] as it a
    requirement of the torch.grid_sample function. The feature parameter learns the best linear mapping between the feature
    map from a given location, sample from Gaussian at train time but set to mean at eval time, and the unit's response with or without an additional elu non-linearity.

    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        outdims (int): number of output units
        bias (bool): adds a bias term
        init_mu_range (float): initialises the the mean with Uniform([-init_range, init_range])
                            [expected: positive value <=1]
        init_sigma_range (float): initialises sigma with Uniform([0.0, init_sigma_range])
        batch_sample (bool): if True, samples a position for each image in the batch separately
                            [default: True as it decreases convergence time and performs just as well]

    """

    def __init__(self, in_shape, outdims, bias, init_mu_range, init_sigma_range, batch_sample=True, **kwargs):

        super().__init__()
        if init_mu_range > 1.0 or init_mu_range <= 0.0 or init_sigma_range <= 0.0:
            raise ValueError("init_mu_range or init_sigma_range is not within required limit!")
        self.in_shape = in_shape
        c, w, h = in_shape
        self.outdims = outdims
        self.batch_sample = batch_sample
        self.grid_shape = (1, outdims, 1, 2)
        self.mu = Parameter(torch.Tensor(*self.grid_shape))  # mean location of gaussian for each neuron
        self.sigma = Parameter(torch.Tensor(*self.grid_shape))  # standard deviation for gaussian for each neuron
        self.features = Parameter(torch.Tensor(1, c, 1, outdims))  # saliency  weights for each channel from core

        if bias:
            bias = Parameter(torch.Tensor(outdims))
            self.register_parameter('bias', bias)
        else:
            self.register_parameter('bias', None)

        self.init_mu_range = init_mu_range
        self.init_sigma_range = init_sigma_range
        self.initialize()

    def initialize(self):
        """
        initialize function initializes the mean, sigma for the Gaussian readout and features weights
        """
        self.mu.data.uniform_(-self.init_mu_range, self.init_mu_range)
        self.sigma.data.uniform_(0, self.init_sigma_range)
        self.features.data.fill_(1 / self.in_shape[0])

        if self.bias is not None:
            self.bias.data.fill_(0)

    def sample_grid(self, batch_size, force_eval_state=False):

        with torch.no_grad():
            self.mu.clamp_(min=-1, max=1)  # at eval time, only self.mu is used so it must belong to [-1,1]
            self.sigma.clamp_(min=0)  # sigma/variance is always a positive quantity
        grid_shape = (batch_size,) + self.grid_shape[1:]

        if self.training and not force_eval_state:
            norm = self.mu.new(*grid_shape).normal_()
        else:
            norm = self.mu.new(*grid_shape).zero_()

        z = norm * self.sigma + self.mu  # grid locations in feature space sampled randomly around the mean self.muq

        return torch.clamp(z, min=-1, max=1)

    @property
    def grid(self):
        return self.sample_grid(batch_size=1, force_eval_state=True)

    def feature_l1(self, average=True):
        """
        feature_l1 function returns the l1 regularization term either the mean or just the sum of weights
        Args:
            average(bool): if True, use mean of weights for regularization
        """
        if average:
            return self.features.abs().mean()
        else:
            return self.features.abs().sum()

    def forward(self, x, shift=None, out_idx=None):
        N, c, w, h = x.size()
        c_in, w_in, h_in = self.in_shape
        if (c_in, w_in, h_in) != (c, w, h):
            raise ValueError("the specified feature map dimension is not the readout's expected input dimension")
        feat = self.features.view(1, c, self.outdims)
        bias = self.bias
        outdims = self.outdims

        if self.batch_sample:
            grid = self.sample_grid(batch_size=N)
        else:
            grid = self.sample_grid(batch_size=1).expand(N, outdims, 1, 2)

        if out_idx is not None:
            if isinstance(out_idx, np.ndarray):
                if out_idx.dtype == bool:
                    out_idx = np.where(out_idx)[0]
            feat = feat[:, :, out_idx]
            grid = grid[:, out_idx, :, :]
            if bias is not None:
                bias = bias[out_idx]
            outdims = len(out_idx)

        if shift is not None:
            grid = grid + shift[:, None, None, :]

        y = F.grid_sample(x, grid)
        y = (y.squeeze(-1) * feat).sum(1).view(N, outdims)

        if self.bias is not None:
            y = y + bias
        return y

    def __repr__(self):
        c, w, h = self.in_shape
        r = self.__class__.__name__ + \
            ' (' + '{} x {} x {}'.format(c, w, h) + ' -> ' + str(self.outdims) + ')'
        if self.bias is not None:
            r += ' with bias'
        for ch in self.children():
            r += '  -> ' + ch.__repr__() + '\n'
        return r


##############
# Pyramid Readout
##############


class MultiplePointPyramid2d(Readout, ModuleDict):

    def __init__(self, in_shape, loaders, gamma_readout, positive, **kwargs):

        super().__init__()

        self.in_shape = in_shape
        self.neurons = OrderedDict([(k, loader.dataset.n_neurons) for k, loader in loaders.items()])
        self._positive = positive
        self.gamma_readout = gamma_readout
        for k, n_neurons in self.neurons.items():
            if isinstance(self.in_shape, dict):
                in_shape = self.in_shape[k]
            self.add_module(k, PointPyramid2d(in_shape=in_shape, outdims=n_neurons, positive=positive, **kwargs))

    @property
    def positive(self):
        return self._positive

    @positive.setter
    def positive(self, value):
        self._positive = value
        for k in self:
            self[k].positive = value

    def initialize(self, mu_dict):

        for k, mu in mu_dict.items():
            self[k].initialize()
            self[k].bias.data = mu.squeeze() - 1

    def regularizer(self, readout_key):
        return self[readout_key].feature_l1() * self.gamma_readout


class Pyramid(nn.Module):
    _filter_dict = {
        'gauss5x5': np.float32([
            [0.003765, 0.015019, 0.023792, 0.015019, 0.003765],
            [0.015019, 0.059912, 0.094907, 0.059912, 0.015019],
            [0.023792, 0.094907, 0.150342, 0.094907, 0.023792],
            [0.015019, 0.059912, 0.094907, 0.059912, 0.015019],
            [0.003765, 0.015019, 0.023792, 0.015019, 0.003765]]),
        'gauss3x3': np.float32([
            [1 / 16, 1 / 8, 1 / 16],
            [1 / 8, 1 / 4, 1 / 8],
            [1 / 16, 1 / 8, 1 / 16]]
        ),
        'laplace5x5': np.outer(np.float32([1, 4, 6, 4, 1]), np.float32([1, 4, 6, 4, 1])) / 256,

    }

    def __init__(self, scale_n=4, type='gauss5x5', downsample=True):
        """
        Setup Laplace image pyramid
        Args:
            scale_n: number of Laplace pyramid layers to construct
            type: type of Gaussian filter used in pyramid construction. Valid options are: 'gauss5x5', 'gauss3x3', and 'laplace5x5'
            downsample: whether to downsample the image in each layer. Defaults to True
        """
        super().__init__()
        self.type = type
        self.downsample = downsample
        h = self._filter_dict[type]
        self.register_buffer('filter', torch.from_numpy(h))
        self.scale_n = scale_n
        self._kern = h.shape[0]
        self._pad = self._kern // 2
        self._filter_cache = None

    def lap_split(self, img):
        N, c, h, w = img.size()
        if self._filter_cache is not None and self._filter_cache.size(0) == c:
            filter = self._filter_cache
        else:
            filter = self.filter.expand(c, 1, self._kern, self._kern).contiguous()
            self._filter_cache = filter

        # the necessary output padding depends on even/odd of the dimension
        output_padding = (h + 1) % 2, (w + 1) % 2

        smooth = F.conv2d(img, filter, padding=self._pad, groups=c)
        if self.downsample:
            lo = smooth[:, :, ::2, ::2]
            lo2 = 4 * F.conv_transpose2d(lo, filter, stride=2, padding=self._pad, output_padding=output_padding,
                                         groups=c)
        else:
            lo = lo2 = smooth

        hi = img - lo2

        return lo, hi

    def forward(self, img):
        levels = []
        for i in range(self.scale_n):
            img, hi = self.lap_split(img)
            levels.append(hi)
        levels.append(img)
        return levels

    def __repr__(self):
        return "Pyramid(scale_n={scale_n}, padding={_pad}, downsample={downsample}, type={type})".format(
            **self.__dict__)


class PointPyramid2d(nn.Module):
    def __init__(self, in_shape, outdims, scale_n, positive, bias,
                 init_range, downsample, type, **kwargs):
        super().__init__()
        self.in_shape = in_shape
        c, w, h = in_shape
        self.outdims = outdims
        self.positive = positive
        self.gauss_pyramid = Pyramid(scale_n=scale_n, downsample=downsample, type=type)
        self.grid = Parameter(torch.Tensor(1, outdims, 1, 2))
        self.features = Parameter(torch.Tensor(1, c * (scale_n + 1), 1, outdims))

        if bias:
            bias = Parameter(torch.Tensor(outdims))
            self.register_parameter('bias', bias)
        else:
            self.register_parameter('bias', None)
        self.init_range = init_range
        self.initialize()

    def initialize(self):
        self.grid.data.uniform_(-self.init_range, self.init_range)
        self.features.data.fill_(1 / self.in_shape[0])

        if self.bias is not None:
            self.bias.data.fill_(0)

    def group_sparsity(self, group_size):
        f = self.features.size(1)
        n = f // group_size
        ret = 0
        for chunk in range(0, f, group_size):
            ret = ret + (self.features[:, chunk:chunk + group_size, ...].pow(2).mean(1) + 1e-12).sqrt().mean() / n
        return ret

    def feature_l1(self, average=True):
        if average:
            return self.features.abs().mean()
        else:
            return self.features.abs().sum()

    def forward(self, x, shift=None):
        if self.positive:
            positive(self.features)
        self.grid.data = torch.clamp(self.grid.data, -1, 1)
        N, c, w, h = x.size()
        m = self.gauss_pyramid.scale_n + 1
        feat = self.features.view(1, m * c, self.outdims)

        if shift is None:
            grid = self.grid.expand(N, self.outdims, 1, 2)
        else:
            grid = self.grid.expand(N, self.outdims, 1, 2) + shift[:, None, None, :]

        pools = [F.grid_sample(xx, grid) for xx in self.gauss_pyramid(x)]
        y = torch.cat(pools, dim=1).squeeze(-1)
        y = (y * feat).sum(1).view(N, self.outdims)

        if self.bias is not None:
            y = y + self.bias
        return y

    def __repr__(self):
        c, w, h = self.in_shape
        r = self.__class__.__name__ + \
            ' (' + '{} x {} x {}'.format(c, w, h) + ' -> ' + str(self.outdims) + ')'
        if self.bias is not None:
            r += ' with bias'

        for ch in self.children():
            r += '  -> ' + ch.__repr__() + '\n'
        return r


##############
# Point Pooled Readout
##############


class MultiplePointPooled2d(Readout, ModuleDict):
    """
    
    "MultiplePointPool2d" instantiates multiple instances of PointPool2d Readouts 
    usually used when dealing with different datasets or areas sharing the same core. 
    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        loaders (list):  a list of dataset objects
        gamma_readout (float): regularisation term for the readout
    """

    def __init__(self, in_shape, loaders, gamma_readout, **kwargs):
        super().__init__()

        self.in_shape = in_shape
        self.neurons = OrderedDict([(k, loader.dataset.n_neurons) for k, loader in loaders.items()])

        self.gamma_readout = gamma_readout  # regularisation strength

        for k, n_neurons in self.neurons.items():  # example: two areas correspond to two readouts
            self.add_module(k, PointPooled2d(in_shape=in_shape, outdims=n_neurons, **kwargs))

    def initialize(self, mean_activity_dict):
        for k, mu in mean_activity_dict.items():
            self[k].initialize()
            self[k].bias.data = mu.squeeze() - 1

    def regularizer(self, readout_key):
        return self[readout_key].feature_l1() * self.gamma_readout


class PointPooled2d(nn.Module):
    def __init__(self, in_shape, outdims, pool_steps, bias, pool_kern, init_range, **kwargs):
        """

        'PointPooled2d' function learns a point in the core feature space for each neuron, with help of torch.grid_sample, that best
        predicts its response. Multiple average pooling steps are applied to reduce search space in each stage and thereby, faster convergence to the best prediction point.

        The readout receives the shape of the core as 'in_shape', number of pooling stages to be performed as 'pool_steps', the kernel size and stride length
        to be used for pooling as 'pool_kern', the number of units/neurons being predicted as 'outdims', 'bias' specifying whether
        or not bias term is to be used and 'init_range' range for initialising the grid with uniform distribution, U(-init_range,init_range).
        The grid parameter contains the normalized locations (x, y coordinates in the core feature space) and is clipped to [-1.1] as it a
        requirement of the torch.grid_sample function. The feature parameter learns the best linear mapping from the pooled feature
        map from a given location to a unit's response with or without an additional elu non-linearity.

        Args:
            in_shape (list): shape of the input feature map [channels, width, height]
            outdims (int): number of output units
            pool_steps (int): number of pooling stages
            bias (bool): adds a bias term
            pool_kern (int): filter size and stride length used for pooling the feature map
            init_range (float): intialises the grid with Uniform([-init_range, init_range])
                                [expected: positive value <=1]

        """
        super().__init__()
        if init_range > 1.0 or init_range <= 0.0:
            raise ValueError("init_range should be a positive number <=1")
        self._pool_steps = pool_steps
        self.in_shape = in_shape
        c, w, h = in_shape
        self.outdims = outdims
        self.grid = Parameter(torch.Tensor(1, outdims, 1, 2))  # x-y coordinates for each neuron
        self.features = Parameter(torch.Tensor(1, c * (self._pool_steps + 1), 1, outdims))
        # weight matrix mapping the core features to the output units

        if bias:
            bias = Parameter(torch.Tensor(outdims))
            self.register_parameter('bias', bias)
        else:
            self.register_parameter('bias', None)

        self.pool_kern = pool_kern
        self.avg = nn.AvgPool2d((pool_kern, pool_kern), stride=pool_kern,
                                count_include_pad=False)  # setup kernel of size=[pool_kern,pool_kern] with stride=pool_kern
        self.init_range = init_range
        self.initialize()

    @property
    def pool_steps(self):
        return self._pool_steps

    @pool_steps.setter
    def pool_steps(self, value):
        assert value >= 0 and int(value) - value == 0, 'new pool steps must be a non-negative integer'
        if value != self._pool_steps:
            print('Resizing readout features')
            c, w, h = self.in_shape
            self._pool_steps = int(value)
            self.features = Parameter(torch.Tensor(1, c * (self._pool_steps + 1), 1, self.outdims))
            self.features.data.fill_(1 / self.in_shape[0])

    def initialize(self):
        """
        initialize function initialises the grid, features or weights and bias terms.
        """
        self.grid.data.uniform_(-self.init_range, self.init_range)
        self.features.data.fill_(1 / self.in_shape[0])

        if self.bias is not None:
            self.bias.data.fill_(0)

    def feature_l1(self, average=True):
        """
        feature_l1 function helps is applying l1 regularization.
        Args:
            average(bool): if True, use mean of weights for regularization

        """
        if average:
            return self.features.abs().mean()
        else:
            return self.features.abs().sum()

    def forward(self, x, shift=None, out_idx=None):
        self.grid.data = torch.clamp(self.grid.data, -1, 1)
        N, c, w, h = x.size()
        c_in, w_in, h_in = self.in_shape
        if [c_in, w_in, h_in] != [c, w, h]:
            raise ValueError("the specified feature map dimension is not the readout's expected input dimension")

        m = self.pool_steps + 1  # the input feature is considered the first pooling stage
        feat = self.features.view(1, m * c, self.outdims)

        if out_idx is None:
            # predict all output units
            grid = self.grid
            bias = self.bias
            outdims = self.outdims
        else:
            # out_idx specifies the indices to subset of units
            feat = feat[:, :, out_idx]
            grid = self.grid[:, out_idx]
            if self.bias is not None:
                bias = self.bias[out_idx]
            outdims = len(out_idx)

        if shift is None:
            grid = grid.expand(N, outdims, 1, 2)
        else:
            # shift grid based on shifter network's prediction
            grid = grid.expand(N, outdims, 1, 2) + shift[:, None, None, :]

        pools = [F.grid_sample(x, grid)]
        for _ in range(self.pool_steps):
            _, _, w_pool, h_pool = x.size()
            if w_pool * h_pool == 1:
                warnings.warn('redundant pooling steps: pooled feature map size is already 1X1, consider reducing it')
            x = self.avg(x)
            pools.append(F.grid_sample(x, grid))
        y = torch.cat(pools, dim=1)
        y = (y.squeeze(-1) * feat).sum(1).view(N, outdims)

        if self.bias is not None:
            y = y + bias
        return y

    def __repr__(self):
        c, w, h = self.in_shape
        r = self.__class__.__name__ + \
            ' (' + '{} x {} x {}'.format(c, w, h) + ' -> ' + str(self.outdims) + ')'
        if self.bias is not None:
            r += ' with bias'
        r += ' and pooling for {} steps\n'.format(self.pool_steps)
        for ch in self.children():
            r += '  -> ' + ch.__repr__() + '\n'
        return r

##############
# Gaussian3d Readout
##############

class MultipleGaussian3d(Readout, ModuleDict):
    """

    "MultipleGaussian3d" instantiates multiple instances of Gaussian3d Readouts
    usually used when dealing with different datasets or areas sharing the same core.
    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        loaders (list):  a list of dataset objects
        gamma_readout (float): regularisation term for the readout which is usally set to 0.0 for gaussian3d readout
                               as it contains one dimensional weight

    """
    def __init__(self, in_shape, loaders, gamma_readout, **kwargs):
        super().__init__()

        self.in_shape = in_shape
        self.neurons = OrderedDict([(k, loader.dataset.n_neurons) for k, loader in loaders.items()])

        self.gamma_readout = gamma_readout

        for k, n_neurons in self.neurons.items():
            self.add_module(k, Gaussian3d(in_shape=in_shape, outdims=n_neurons, **kwargs))

    def initialize(self, mean_activity_dict):
        for k, mu in mean_activity_dict.items():
            self[k].initialize()
            self[k].bias.data = mu.squeeze() - 1

    def regularizer(self, readout_key):
        return self.gamma_readout


class Gaussian3d(nn.Module):
    """

    'Gaussian3d' class instantiates an object that can used to learn a point in the core feature space for each neuron,
    sampled from a Gaussian distribution with some mean and variance at train but set to mean at test time, that best predicts its response.

    The readout receives the shape of the core as 'in_shape', the number of units/neurons being predicted as 'outdims', 'bias' specifying whether
    or not bias term is to be used and 'init_range' range for initialising the mean and variance of the gaussian distribution from which we sample to
    uniform distribution, U(-init_mu_range,init_mu_range) and  uniform distribution, U(0.0, init_sigma_range) respectively.
    The grid parameter contains the normalized locations (x, y coordinates in the core feature space) and is clipped to [-1.1] as it a
    requirement of the torch.grid_sample function. The feature parameter learns the best linear mapping between the feature
    map from a given location, sample from Gaussian at train time but set to mean at eval time, and the unit's response with or without an additional elu non-linearity.

    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        outdims (int): number of output units
        bias (bool): adds a bias term
        init_mu_range (float): initialises the the mean with Uniform([-init_range, init_range])
                            [expected: positive value <=1]
        init_sigma_range (float): initialises sigma with Uniform([0.0, init_sigma_range])
        batch_sample (bool): if True, samples a position for each image in the batch separately
                            [default: True as it decreases convergence time and performs just as well]

    """

    def __init__(self, in_shape, outdims, bias, init_mu_range, init_sigma_range, batch_sample, **kwargs):
        super().__init__()
        if init_mu_range > 1.0 or init_mu_range <= 0.0 or init_sigma_range <= 0.0:
            raise ValueError("init_mu_range or init_sigma_range is not within required limit!")
        self.in_shape = in_shape
        c, w, h = in_shape
        self.outdims = outdims
        self.batch_sample = batch_sample
        self.grid_shape = (1, 1, outdims, 1, 3)
        self.mu = Parameter(torch.Tensor(*self.grid_shape))  # mean location of gaussian for each neuron
        self.sigma = Parameter(torch.Tensor(*self.grid_shape))  # standard deviation for gaussian for each neuron
        self.features = Parameter(torch.Tensor(1, 1, 1, outdims))  # saliency weights for each channel from core

        if bias:
            bias = Parameter(torch.Tensor(outdims))
            self.register_parameter('bias', bias)
        else:
            self.register_parameter('bias', None)

        self.init_mu_range = init_mu_range
        self.init_sigma_range = init_sigma_range
        self.initialize()

    def sample_grid(self, batch_size, force_eval_state=False):

        with torch.no_grad():
            self.mu.clamp_(min=-1, max=1)  # at eval time, only self.mu is used so it must belong to [-1,1]
            self.sigma.clamp_(min=0)  # sigma/variance is always a positive quantity
        grid_shape = (batch_size,) + self.grid_shape[1:]

        if self.training and not force_eval_state:
            norm = self.mu.new(*grid_shape).normal_()
        else:
            norm = self.mu.new(*grid_shape).zero_()

        z = norm * self.sigma + self.mu  # grid locations in feature space sampled randomly around the mean self.muq

        return torch.clamp(z, min=-1, max=1)

    @property
    def grid(self):
        return self.sample_grid(batch_size=1, force_eval_state=True)

    def initialize(self):

        self.mu.data.uniform_(-self.init_mu_range, self.init_mu_range)
        self.sigma.data.uniform_(0, self.init_sigma_range)
        self.features.data.fill_(1 / self.in_shape[0])

        if self.bias is not None:
            self.bias.data.fill_(0)

    def forward(self, x, shift=None, out_idx=None):

        N, c, w, h = x.size()
        c_in, w_in, h_in = self.in_shape
        if (c_in, w_in, h_in) != (c, w, h):
            raise ValueError("the specified feature map dimension is not the readout's expected input dimension")
        x = x.view(N, 1, c, w, h)
        feat = self.features
        bias = self.bias
        outdims = self.outdims

        if self.batch_sample:
            grid = self.sample_grid(batch_size=N)
        else:
            grid = self.sample_grid(batch_size=1).expand(N, 1, outdims, 1, 3)

        if out_idx is not None:
        # out_idx specifies the indices to subset of neurons for training/testing
            if isinstance(out_idx, np.ndarray):
                if out_idx.dtype == bool:
                    out_idx = np.where(out_idx)[0]
            feat = feat[:, :, :, out_idx]
        grid = grid[:, :, out_idx]
            if bias is not None:
                bias = bias[out_idx]
            outdims = len(out_idx)

        if shift is not None:
            grid = grid + shift[:, None, None, :]

        y = F.grid_sample(x, grid)
        y = (y.squeeze(-1) * feat).sum(1).view(N,outdims)

        if self.bias is not None:
            y = y + bias
        return y



#############
# UltraSparse Readout
#############

class MultipleUltraSparse(Readout, ModuleDict):
    """

    "MultipleUltraSparse" instantiates multiple instances of UltraSparseReadout
    usually used when dealing with different datasets or areas sharing the same core.
    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        loaders (list):  a list of dataset objects
        gamma_readout (float): regularisation term for the readout which is usally set to 0.0 for UltraSparseReadout readout
                               as it contains one dimensional weight

    """
    def __init__(self, in_shape, loaders, gamma_readout, **kwargs):
        super().__init__()

        self.in_shape = in_shape
        self.neurons = OrderedDict([(k, loader.dataset.n_neurons) for k, loader in loaders.items()])

        self.gamma_readout = gamma_readout

        for k, n_neurons in self.neurons.items():
            self.add_module(k, UltraSparse(in_shape=in_shape, outdims=n_neurons, **kwargs))

    def initialize(self, mean_activity_dict):
        for k, mu in mean_activity_dict.items():
            self[k].initialize()
            self[k].bias.data = mu.squeeze() - 1

    def regularizer(self, readout_key):
        return self.gamma_readout

class UltraSparse(nn.Module):
    """

    'UltraSparse' class instantiates an object that can used to learn one or more features (with or without
    a shared mean in the x-y plane) in the core feature space for each neuron, sampled from a Gaussian distribution
    with some mean and variance at training but set to mean at test time, that best predicts its response.

    The readout receives the shape of the core as 'in_shape', the number of units/neurons being predicted as 'outdims', 'bias' specifying whether
    or not bias term is to be used and 'init_range' range for initialising the mean and variance of the gaussian distribution from which we sample to
    uniform distribution, U(-init_mu_range,init_mu_range) and  uniform distribution, U(0.0, init_sigma_range) respectively.
    The grid parameter contains the normalized locations (x, y coordinates in the core feature space) and is clipped to [-1.1] as it a
    requirement of the torch.grid_sample function. The feature parameter learns the best linear mapping between the feature
    map from a given location, sample from Gaussian at train time but set to mean at eval time, and the unit's response with or without an additional elu non-linearity.

    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        outdims (int): number of output units
        bias (bool): adds a bias term
        init_mu_range (float): initialises the the mean with Uniform([-init_range, init_range])
                            [expected: positive value <=1]
        init_sigma_range (float): initialises sigma with Uniform([0.0, init_sigma_range])
        batch_sample (bool): if True, samples a position for each image in the batch separately
                            [default: True as it decreases convergence time and performs just as well]
        num_filters (int): number of points in the core-features to be learned for each neuron
                           [default: 1, an instance of sparsest readout]
        shared_mean (bool): if True, the mean in the x-y plane (image-plane) is shared across the different filters
                           [default: False]


    """

    def __init__(self, in_shape, outdims, bias, init_mu_range, init_sigma_range, batch_sample=True, num_filters=1,
                 shared_mean=False, **kwargs):

        super().__init__()
        if init_mu_range > 1.0 or init_mu_range <= 0.0 or init_sigma_range <= 0.0:
            raise ValueError("init_mu_range or init_sigma_range is not within required limit!")
        self.in_shape = in_shape
        c, w, h = in_shape
        self.outdims = outdims
        self.batch_sample = batch_sample
        self.num_filters = num_filters
        self.shared_mean = shared_mean
        self.grid_shape = (1, 1, outdims * num_filters, 1, 3)

        if shared_mean:

            self.gridx_shape = (1, 1, outdims, 1, 2)
            self.gridc_shape = (1, 1, outdims * num_filters, 1, 1)
            self.mux = Parameter(torch.Tensor(*self.gridx_shape))  # mean location of gaussian for each neuron
            self.muc = Parameter(torch.Tensor(*self.gridc_shape))
            self.sigmax = Parameter(torch.Tensor(*self.gridx_shape))  # standard deviation for gaussian for each neuron
            self.sigmac = Parameter(torch.Tensor(*self.gridc_shape))

        else:

            self.mu = Parameter(torch.Tensor(*self.grid_shape))  # mean location of gaussian for each neuron
            self.sigma = Parameter(torch.Tensor(*self.grid_shape))  # standard deviation for gaussian for each neuron

        self.features = Parameter(
            torch.Tensor(1, 1, outdims, num_filters))  # saliency  weights for each channel from core

        if bias:
            bias = Parameter(torch.Tensor(outdims))
            self.register_parameter('bias', bias)
        else:
            self.register_parameter('bias', None)

        self.init_mu_range = init_mu_range
        self.init_sigma_range = init_sigma_range
        self.initialize()

    def sample_grid(self, batch_size, force_eval_state=False):

        if self.shared_mean:
            self.mu = torch.cat((self.mux.repeat(1, 1, self.num_filters, 1, 1), self.muc), 4)
            self.sigma = torch.cat((self.sigmax.repeat(1, 1, self.num_filters, 1, 1), self.sigmac), 4)

        with torch.no_grad():
            self.mu.clamp_(min=-1, max=1)  # at eval time, only self.mu is used so it must belong to [-1,1]
            self.sigma.clamp_(min=0)  # sigma/variance is always a positive quantity
        grid_shape = (batch_size,) + self.grid_shape[1:]

        if self.training and not force_eval_state:
            norm = self.mu.new(*grid_shape).normal_()
        else:
            norm = self.mu.new(*grid_shape).zero_()

        z = norm * self.sigma + self.mu  # grid locations in feature space sampled randomly around the mean self.muq

        return torch.clamp(z, min=-1, max=1)

    @property
    def grid(self):
        return self.sample_grid(batch_size=1, force_eval_state=True)

    def initialize(self):

        if self.shared_mean:

            self.muc.data.uniform_(-1, 1)
            self.sigmac.data.uniform_(0, self.init_sigma_range)
            self.mux.data.uniform_(-self.init_mu_range, self.init_mu_range)
            self.sigmax.data.uniform_(0, self.init_sigma_range)

        else:

            self.mu.data.uniform_(-self.init_mu_range, self.init_mu_range)
            self.sigma.data.uniform_(0, self.init_sigma_range)

        self.features.data.fill_(1 / self.in_shape[0])

        if self.bias is not None:
            self.bias.data.fill_(0)

    def forward(self, x, shift=None, out_idx=None):

        N, c, w, h = x.size()
        c_in, w_in, h_in = self.in_shape
        if (c_in, w_in, h_in) != (c, w, h):
            raise ValueError("the specified feature map dimension is not the readout's expected input dimension")
        x = x.view(N, 1, c, w, h)
        feat = self.features
        bias = self.bias
        outdims = self.outdims

        if self.batch_sample:
            grid = self.sample_grid(batch_size=N)
        else:
            grid = self.sample_grid(batch_size=1).expand(N, 1, outdims * self.num_filters, 1, 3)

        if out_idx is not None:
            if isinstance(out_idx, np.ndarray):
                if out_idx.dtype == bool:
                    out_idx = np.where(out_idx)[0]
            feat = feat[:, :, :, out_idx]
            grid = grid[:, :, out_idx]
            if bias is not None:
                bias = bias[out_idx]
            outdims = len(out_idx)

        if shift is not None:  # it might not be valid now but have kept it for future devop.
            grid = grid + shift[:, None, None, :]

        y = F.grid_sample(x, grid).squeeze(-1)
        z = y.view((N, 1, self.num_filters, outdims)).permute(0, 1, 3, 2) # reorder the dims
        z = torch.einsum('nkpf,mkpf->np', z, feat) # dim: batch_size, 1, num_neurons, num_filters -> batch_size, num_neurons


        if self.bias is not None:
            z = z + bias
        return z

    def __repr__(self):
        c, w, h = self.in_shape
        r = self.__class__.__name__ + \
            ' (' + '{} x {} x {}'.format(c, w, h) + ' -> ' + str(self.outdims) + ')'
        if self.bias is not None:
            r += ' with bias'
        for ch in self.children():
            r += '  -> ' + ch.__repr__() + '\n'
        return r