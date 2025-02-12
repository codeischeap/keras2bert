from keras2bert.backend import keras, K, mask_sequences
from keras.layers import *
import tensorflow as tf


class Layer(keras.layers.Layer):
    """重定义层，支持mask
    """
    def __init__(self, **kwargs):
        super(Layer, self).__init__(**kwargs)
        self.supports_masking = True


keras.layers.Layer = Layer


class TokenEmbedding(keras.layers.Embedding):
    """重新定义可返回权重的Embedding层
    """
    def compute_output_shape(self, input_shape):
        return [super(TokenEmbedding, self).compute_output_shape(input_shape), (self.input_dim, self.output_dim)]

    def compute_mask(self, inputs, mask=None):
        return [super(TokenEmbedding, self).compute_mask(inputs, mask), None]

    def call(self, inputs):
        return [super(TokenEmbedding, self).call(inputs), self.embeddings]


class PositionEmbedding(keras.layers.Layer):
    """位置编码
    支持5种模式
       * Expand mode: negative integers (relative position) could be used in this mode.
       * Rel mode
       * Add mode
       * Mul mode
       * Concat mode
    """
    MODE_EXPAND = 'expand'
    MODE_REL = 'relative'
    MODE_ADD = 'add'
    MODE_MUL = 'mul'
    MODE_CONCAT = 'concat'

    def __init__(self,
                 input_dim,
                 output_dim,
                 mode=MODE_ADD,
                 embedding_initializer='uniform',
                 embedding_regularizer=None,
                 embedding_constraint=None,
                 mask_zero=False,
                 **kwargs):
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.mode = mode
        self.embedding_initializer = keras.initializers.get(embedding_initializer)
        self.embeddings_regularizer = keras.regularizers.get(embedding_regularizer)
        self.embeddings_constraint = keras.constraints.get(embedding_constraint)
        self.mask_zero = mask_zero
        self.embeddings = None
        super(PositionEmbedding, self).__init__(**kwargs)

    def build(self, input_shape):
        if self.mode == self.MODE_EXPAND:
            self.embeddings = self.add_weight(
                shape=(self.input_dim * 2 + 1, self.output_dim),
                initializer=self.embedding_initializer,
                name='pos_embeddings',
            )
        elif self.mode == self.MODE_REL:
            self.embeddings = self.add_weight(
                shape=(self.input_dim, self.output_dim),
                initializer=self.embedding_initializer,
                trainable=False,
                name='relative_embeddings',
            )
        else:
            self.embeddings = self.add_weight(
                shape=(self.input_dim, self.output_dim),
                initializer=self.embedding_initializer,
                name='pos_embeddings'
            )
        super(PositionEmbedding, self).build(input_shape)

    def call(self, inputs, **kwargs):
        if self.mode == self.MODE_REL:
            i_index = K.arange(0, K.shape(inputs)[1], dtype='int32')
            j_index = K.arange(0, K.shape(inputs)[1], dtype='int32')
            pos_index = i_index[None, :] - j_index[:, None]
            max_pos = (self.input_dim - 1) // 2
            return K.gather(
                self.embeddings,
                K.minimum(K.maximum(pos_index, -max_pos), max_pos) + max_pos
            )
        elif self.mode == self.MODE_EXPAND:
            if K.dtype(inputs) != 'int32':
                inputs = K.cast(inputs, 'int32')
            return K.gather(
                self.embeddings,
                K.minimum(K.maximum(inputs, -self.input_dim), self.input_dim) + self.input_dim
            )
        input_shape = K.shape(inputs)
        batch_size, seq_len = input_shape[0], input_shape[1]
        pos_embeddings = K.tile(
            K.expand_dims(self.embeddings[:seq_len], axis=0),
            [batch_size, 1, 1],
        )
        if self.mode == self.MODE_ADD:
            return inputs + pos_embeddings
        if self.mode == self.MODE_MUL:
            return inputs * (1. + pos_embeddings)
        return K.concatenate([inputs, pos_embeddings], axis=-1)

    def compute_mask(self, inputs, mask=None):
        if self.mode == self.MODE_EXPAND:
            if self.mask_zero:
                output_mask = K.not_equal(inputs, 0)
            else:
                output_mask = None
        else:
            output_mask = mask
        return output_mask

    def compute_output_shape(self, input_shape):
        if self.mode == self.MODE_REL:
            return input_shape[:-1] + (self.output_dim,)
        if self.mode == self.MODE_EXPAND:
            return input_shape + (self.output_shape,)
        if self.mode == self.MODE_CONCAT:
            return input_shape[:-1] + (input_shape[-1] + self.output_dim,)
        return input_shape

    def get_config(self):
        config = {
            "input_dim": self.input_dim,
            "out_dim": self.output_dim,
            "mode": self.mode,
            "embedding_initializer": keras.initializers.serialize(self.embedding_initializer),
            "embedding_regularizer": keras.initializers.serialize(self.embeddings_regularizer),
            "embedding_constraint": keras.initializers.serialize(self.embeddings_constraint),
            "mask_zero": self.mask_zero,
        }
        base_config = super(PositionEmbedding, self).get_config()
        config.update(base_config)
        return config


class RelativePositionBias(keras.layers.Layer):
    """Google T5的相对位置偏置
     # Reference:
        [Exploring the Limits of Transfer Learning with a Unified Text-to-Text Transformer]
        (https://arxiv.org/abs/1910.10683)
    """
    def __init__(self,
                 birectional=True,
                 num_buckets=32,
                 max_distance=128,
                 n_heads=2,
                 embedding_initializer='uniform',
                 **kwargs):
        super(RelativePositionBias, self).__init__(**kwargs)
        self.bidirectional = birectional
        self.num_buckets = num_buckets
        self.max_distance = max_distance
        self.n_heads = n_heads
        self.embedding_initializer = embedding_initializer

    def build(self, input_shape):
        super(RelativePositionBias, self).build(input_shape)
        self.embeddings = self.add_weight(
            shape=(self.num_buckets, self.n_heads),
            initializer=self.embedding_initializer,
            trainable=True,
            name='relative_embeddings_t5',
        )

    def call(self, inputs, mask=None, **kwargs):
        """
                   k
             0   1   2   3
        q   -1   0   1   2
            -2  -1   0   1
            -3  -2  -1   0
        """
        q, k = inputs
        q_index = K.arange(0, K.shape(q)[1], dtype='int32')
        k_index = K.arange(0, K.shape(k)[1], dtype='int32')
        relative_position = k_index[None, :] - q_index[:, None]
        num_buckets = self.num_buckets
        max_distance = self.max_distance

        ret = 0
        n = -relative_position
        if self.bidirectional:
            num_buckets //= 2
            ret += K.cast(K.less(n, 0), 'int32') * num_buckets
            n = K.abs(n)
        else:
            n = K.max(n, K.zeros_like(n))
        # now n is in the range [0, inf)
        # half of the buckets are for exact increments in positions
        max_exact = num_buckets // 2
        is_small = K.less(n, max_exact)
        # The other half of the buckets are for logarithmically bigger bins in positions up to max_distance
        val_if_large = max_exact + K.cast(
            K.log(K.cast(n, K.floatx()) / max_exact) / K.log(max_distance / max_exact)
            * (num_buckets - max_exact),
            'int32'
        )
        val_if_large = K.minimum(val_if_large, num_buckets - 1)
        ret += K.switch(is_small, n, val_if_large)
        values = K.gather(
            self.embeddings,
            ret,
        ) # shape (qlen, klen, num_heads)
        values = K.expand_dims(
            K.permute_dimensions(values, [2, 0, 1]),
            0
        ) # shape (1, num_heads, qlen, klen)
        return values

    def compute_mask(self, inputs, mask=None):
        return mask[0]

    def compute_output_shape(self, input_shape):
        return (None, None, self.n_heads)

    def get_config(self):
        config = {
            "bidirectional": self.bidirectional,
            "max_distance": self.max_distance,
            "num_buckets": self.num_buckets,
            "n_heads": self.n_heads,
            "embedding_initializer": self.embedding_initializer,
        }
        base_config = super(RelativePositionBias, self).get_config()
        config.update(base_config)
        return config


class MultiHeadSelfAttention(keras.layers.Layer):
    """多头自注意力机制
    # Reference:
        [Attention Is All You Need]
        (https://arxiv.org/abs/1706.03762)
    """
    def __init__(self,
                 head_num,
                 query_size,
                 key_size,
                 output_dim,
                 use_bias=True,
                 attention_dropout_rate=0.0,
                 kernel_initializer='glorot_uniform',
                 **kwargs):
        self.head_num = head_num
        self.query_size = query_size
        self.key_size = key_size
        self.feature_dim = output_dim
        self.use_bias = use_bias
        self.attention_dropout_rate = attention_dropout_rate
        self.kernel_initializer = kernel_initializer
        super(MultiHeadSelfAttention, self).__init__(**kwargs)

    def build(self, input_shape):
        super(MultiHeadSelfAttention, self).build(input_shape)

        self.q_dense = Dense(
            self.head_num * self.query_size,
            use_bias=self.use_bias,
            kernel_initializer=self.kernel_initializer
        )
        self.k_dense = Dense(
            self.head_num * self.query_size,
            use_bias=self.use_bias,
            kernel_initializer=self.kernel_initializer,
        )
        self.v_dense = Dense(
            self.head_num * self.key_size,
            use_bias=self.use_bias,
            kernel_initializer=self.kernel_initializer,
        )
        self.o_dense = Dense(
            self.feature_dim,
            use_bias=self.use_bias,
            kernel_initializer=self.kernel_initializer,
        )

    def call(self, inputs, mask=None):
        qw = self.q_dense(inputs[0])
        kw = self.k_dense(inputs[1])
        vw = self.v_dense(inputs[2])

        qw = K.reshape(qw, (-1, K.shape(qw)[1], self.head_num, self.query_size))
        kw = K.reshape(kw, (-1, K.shape(kw)[1], self.head_num, self.query_size))
        vw = K.reshape(vw, (-1, K.shape(vw)[1], self.head_num, self.key_size))

        a = tf.einsum('bmhd, bnhd->bhmn', qw, kw)
        a = a / self.query_size ** 0.5
        a = mask_sequences(a, mask[1], axis=-1, value='-inf')

        # 将attention score归一化成概率分布
        a = K.softmax(a, axis=-1)
        # 这里的dropout参考自google transformer论文
        a = keras.layers.Dropout(self.attention_dropout_rate)(a)
        o = tf.einsum('bhmn, bnhd->bmhd', a, vw)

        o = K.reshape(o, (-1, K.shape(o)[1], self.head_num * self.key_size))
        o = self.o_dense(o)

        return o

    def compute_mask(self, inputs, mask=None):
        return mask[0]

    def compute_output_shape(self, input_shape):
        o_shape = input_shape[0][:-1] + (self.feature_dim,)
        return o_shape

    def get_config(self):
        config = {
            "head_num": self.head_num,
            "query_size": self.query_size,
            "key_size": self.key_size,
            "feature_dim": self.feature_dim,
        }
        base_config = super(MultiHeadSelfAttention, self).get_config()
        config.update(base_config)
        return config


class FeedForward(keras.layers.Layer):
    """逐位置前馈层
    # Reference:
        [Attention Is All You Need]
        (https://arxiv.org/abs/1706.03762)
    """
    def __init__(self,
                 units,
                 activation='relu',
                 kernel_initializer='glorot_normal',
                 regularizer=None,
                 constraint=None,
                 use_bias=True,
                 dropout_rate=0.0,
                 **kwargs):
        super(FeedForward, self).__init__(**kwargs)
        self.units = units
        if not isinstance(activation, list):
            activation = [activation]
        self.activation = [keras.activations.get(act) for act in activation]
        self.kernel_initializer = keras.initializers.get(kernel_initializer)
        self.regularizer = keras.regularizers.get(regularizer)
        self.constraint = keras.constraints.get(constraint)
        self.use_bias = use_bias
        self.dropout_rate = dropout_rate

    def build(self, input_shape):
        super(FeedForward, self).build(input_shape)
        output_dim = input_shape[-1]
        for i in range(len(self.activation)):
            self.h_dense = keras.layers.Dense(
                units=self.units,
                use_bias=self.use_bias,
                activation=self.activation[i],
                kernel_initializer=self.kernel_initializer,
            )
            setattr(self, 'h_dense_%i' % i, self.h_dense)
        self.o_dense = keras.layers.Dense(
            units=output_dim,
            use_bias=self.use_bias,
            kernel_initializer=self.kernel_initializer
        )

    def call(self, inputs, **kwargs):
        h = getattr(self, 'h_dense_0')(inputs)
        for i in range(1, len(self.activation)):
            h = h * getattr(self, 'h_dense_%i' %i)(inputs)
            if 0 < self.dropout_rate < 1.0:
                h = keras.layers.Dropout(rate=self.dropout_rate)(h)
        o = self.o_dense(h)
        return o

    def compute_output_shape(self, input_shape):
        return input_shape

    def compute_mask(self, inputs, mask=None):
        return mask

    def get_config(self):
        config = {
            "units": self.units,
            "activation": keras.activations.serialize(self.activation),
            "kernel_initializer": keras.initializers.serialize(self.kernel_initializer),
            "regularizer": keras.regularizers.serialize(self.regularizer),
            "constraint": keras.constraints.serialize(self.constraint),
            "use_bias": self.use_bias,
            "dropout_rate": self.dropout_rate,
        }
        base_config = super(FeedForward, self).get_config()
        config.update(base_config)
        return config


class LayerNormalization(keras.layers.Layer):
    """层归一化
    # Reference:
        [Layer Normalization]
        (https://arxiv.org/pdf/1607.06450.pdf)
    """
    def __init__(self,
                 center=True,
                 scale=True,
                 episilon=None,
                 **kwargs):
        super(LayerNormalization, self).__init__(**kwargs)
        self.center = center
        self.scale = scale
        if episilon is None:
            episilon = K.epsilon() * K.epsilon()
        self.episilon = episilon

    def build(self, input_shape):
        super(LayerNormalization, self).build(input_shape)
        if self.scale:
            self.gamma = self.add_weight(name='gamma',
                                         shape=(input_shape[-1],),
                                         dtype='float32',
                                         initializer='ones')
        if self.center:
            self.beta = self.add_weight(name='beta',
                                        shape=(input_shape[-1],),
                                        dtype='float32',
                                        initializer='zeros')

    def call(self, inputs, **kwargs):
        mean = K.mean(inputs, axis=-1, keepdims=True)
        inputs = inputs - mean
        variance = K.mean(K.square(inputs), axis=-1, keepdims=True)
        outputs = inputs / K.sqrt(variance + self.episilon)

        if self.scale:
            outputs *= self.gamma
        if self.center:
            outputs += self.beta
        return outputs

    def compute_output_shape(self, input_shape):
        return input_shape

    def compute_mask(self, inputs, mask=None):
        return mask

    def get_config(self):
        config = {
            "center": self.center,
            "scale": self.scale,
            "episilon": self.episilon,
        }
        base_config = super(LayerNormalization, self).get_config()
        config.update(base_config)
        return config


class EmbeddingSimilarity(keras.layers.Layer):
    """用于输出特征与输入embedding矩阵的相似度计算
    """
    def __init__(self,
                 initializer='zeros',
                 regularizer=None,
                 constraint=None,
                 use_bias=True,
                 **kwargs):
        super(EmbeddingSimilarity, self).__init__(**kwargs)
        self.use_bias = use_bias
        self.initializer = keras.initializers.get(initializer)
        self.regularizer = keras.regularizers.get(regularizer)
        self.constraint = keras.constraints.get(constraint)

    def build(self, input_shape):
        super(EmbeddingSimilarity, self).build(input_shape)
        if self.use_bias:
            self.bias = self.add_weight(name='bias',
                                        shape=(input_shape[1][0],),
                                        initializer=self.initializer)

    def call(self, inputs, mask=None):
        inputs, embeddings = inputs
        if self.use_bias:
            output = K.bias_add(K.dot(inputs, K.transpose(embeddings)), self.bias)
        else:
            output = K.dot(inputs, K.transpose(embeddings))

        return keras.activations.softmax(output)

    def compute_output_shape(self, input_shape):
        return input_shape[0][:-1] + (input_shape[1][0],)

    def compute_mask(self, inputs, mask=None):
        return mask[0]

    def get_config(self):
        config = {
            "initializer": keras.initializers.serialize(self.initializer),
            "regularizer": keras.regularizers.serialize(self.regularizer),
            "constraint": keras.constraints.serialize(self.constraint),
            "use_bias": self.use_bias
        }
        base_config = super(EmbeddingSimilarity, self).get_config()
        config.update(base_config)
        return config


class Scale(keras.layers.Layer):
    """用于特征缩放
    """
    def __init__(self, scale, **kwargs):
        self.scale = scale
        super(Scale, self).__init__(**kwargs)

    def call(self, inputs, mask=None, **kwargs):
        return inputs * self.scale

    def get_config(self):
        config = {
            "scale": self.scale,
        }
        base_config = super(Scale, self).get_config()
        config.update(base_config)
        return config


class Loss(keras.layers.Layer):
    """自定义损失层, 可以用来定义复杂的损失，比如Dice Loss
    """
    def __init__(self, output_dims=None, **kwargs):
        super(Loss, self).__init__(**kwargs)
        self.output_dims = output_dims

    def call(self, inputs, mask=None):
        loss = self.compute_loss(inputs, mask)
        self.add_loss(loss)
        if self.output_dims is None:
            return inputs
        elif isinstance(self.output_dims, (list, tuple)):
            return [inputs[i] for i in self.output_dims]
        else:
            return inputs[self.output_dims]

    def compute_loss(self, inputs, mask=None):
        raise NotImplementedError

    def compute_mask(self, inputs, mask=None):
        if mask is not None:
            if self.output_dims is None:
                return mask
            elif isinstance(self.output_dims, (list, tuple)):
                return [mask[i] for i in self.output_dims]
            else:
                return mask[self.output_dims]
        return mask

    def compute_output_shape(self, input_shape):
        if self.output_dims is None:
            return input_shape
        elif isinstance(self.output_dims, (list, tuple)):
            return [input_shape[i] for i in self.output_dims]
        else:
            return input_shape[self.output_dims]

    def get_config(self):
        config = {
            "output_dims": self.output_dims
        }
        base_config = super(Loss, self).get_config()
        config.update(base_config)
        return config