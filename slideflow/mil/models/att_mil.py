import torch
from torch import nn
from typing import Optional, List

from slideflow.model.torch_utils import get_device

# -----------------------------------------------------------------------------

class Attention_MIL(nn.Module):
    """Attention-based multiple instance learning model.

    Implementation from: https://github.com/KatherLab/marugoto

    """
    def __init__(
        self,
        n_feats: int,
        n_out: int,
        z_dim: int = 256,
        dropout_p: float = 0.5,
        encoder: Optional[nn.Module] = None,
        attention: Optional[nn.Module] = None,
        head: Optional[nn.Module] = None,
    ) -> None:
        """Create a new attention MIL model.
        Args:
            n_feats:  The number of features each bag instance has.
            n_out:  The number of output layers of the model.
            encoder:  A network transforming bag instances into feature vectors.
        """
        super().__init__()
        self.encoder = encoder or nn.Sequential(nn.Linear(n_feats, z_dim), nn.ReLU())
        self.attention = attention or Attention(z_dim)
        self.head = head or nn.Sequential(
            nn.Flatten(), nn.BatchNorm1d(z_dim), nn.Dropout(dropout_p), nn.Linear(z_dim, n_out)
        )

    def forward(self, bags, lens):
        # bags: B x N_max x F
        # lens: B x 1 (?)
        assert bags.ndim == 3
        assert bags.shape[0] == lens.shape[0]

        embeddings = self.encoder(bags) # -> B x N_max x Z

        masked_attention_scores = self._masked_attention_scores(embeddings, lens)   # -> B x N_max x 1
        weighted_embedding_sums = (masked_attention_scores * embeddings).sum(-2)    # -> B x Z

        scores = self.head(weighted_embedding_sums) # -> B x C

        return scores

    def calculate_attention(self, bags, lens):
        embeddings = self.encoder(bags)
        return self._masked_attention_scores(embeddings, lens)

    def _masked_attention_scores(self, embeddings, lens):
        """Calculates attention scores for all bags.
        Returns:
            A tensor containing torch.concat([torch.rand(64, 256), torch.rand(64, 23)], -1)
             *  The attention score of instance i of bag j if i < len[j]
             *  0 otherwise
        """
        # embeddings: B x N_max x Z
        bs, bag_size = embeddings.shape[0], embeddings.shape[1]
        attention_scores = self.attention(embeddings)   # -> B x N_max x 1

        # a tensor containing a row [0, ..., bag_size-1] for each batch instance
        idx = torch.arange(bag_size).repeat(bs, 1).to(attention_scores.device)

        # False for every instance of bag i with index(instance) >= lens[i]
        attention_mask = (idx < lens.unsqueeze(-1)).unsqueeze(-1)

        masked_attention = torch.where(
            attention_mask, attention_scores, torch.full_like(attention_scores, -torch.inf)
        )
        return torch.softmax(masked_attention, dim=1)   # -> B x N_max x 1

    def relocate(self):
        """Move model to GPU. Required for FastAI compatibility."""
        self.to(get_device())

    def plot(*args, **kwargs):
        pass

# -----------------------------------------------------------------------------

def Attention(n_in: int, n_latent: Optional[int] = None) -> nn.Module:
    """A network calculating an embedding's importance weight."""
    # Note: softmax not being applied here, as it will be applied later,
    # after masking out the padding.
    if n_latent == 0:
        return nn.Linear(n_in, 1)
    else:
        n_latent = n_latent or (n_in + 1) // 2
        return nn.Sequential(
            nn.Linear(n_in, n_latent),
            nn.Tanh(),
            nn.Linear(n_latent, 1)
        )

# -----------------------------------------------------------------------------

class MultiModal_Attention_MIL(nn.Module):
    """Attention-based MIL model for multiple input feature spaces.

    Used for multi-magnification MIL. Differs from Attention_MIL in that it
    takes multiple bags as input, one for each magnification.

    """

    multimodal = True

    def __init__(
        self,
        n_feats: List[int],
        n_out: int,
        z_dim: int = 512,
        dropout_p: float = 0.3
    ) -> None:
        super().__init__()
        self.n_input = len(n_feats)
        self._z_dim = z_dim
        self._dropout_p = dropout_p
        self._n_out = n_out
        for i in range(self.n_input):
            setattr(self, f'encoder_{i}', nn.Sequential(nn.Linear(n_feats[i], z_dim), nn.ReLU()))
            setattr(self, f'attention_{i}', Attention(z_dim, n_latent=0))  # Simple, single-layer attention
            setattr(self, f'prehead_{i}', nn.Sequential(nn.Flatten(),
                                                        nn.BatchNorm1d(z_dim),
                                                        nn.ReLU(),
                                                        nn.Dropout(dropout_p)))

        # Concatenate the weighted sums of embeddings from each magnification
        # into a single vector, then pass it through a linear layer.
        self.head = nn.Sequential(
            nn.Linear(z_dim * self.n_input, z_dim),
            nn.LayerNorm(z_dim),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(z_dim, n_out)
        )

    def forward(self, *bags_and_lens):
        """Return predictions using all bags and magnifications.

        Input should be a list of tuples, with each tuple containing a bag and
        lens tensors corresponding to a single magnification level. The length
        of the list should be equal to the number of magnification levels.

        """
        self._verify_input(*bags_and_lens)
        bags, lenses = zip(*bags_and_lens)

        embeddings = self._calculate_embeddings(bags)
        masked_attention_scores = self._all_masked_attention(embeddings, lenses)

        weighted_embeddings = self._calculate_weighted_embeddings(masked_attention_scores, embeddings)
        merged_embeddings = torch.cat(weighted_embeddings, dim=1)

        output = self.head(merged_embeddings)
        return output

    def calculate_attention(self, *bags_and_lens):
        """Calculate attention scores for all bags and magnifications."""
        self._verify_input(*bags_and_lens)
        bags, lenses = zip(*bags_and_lens)

        # Convert bags into embeddings.
        embeddings = self._calculate_embeddings(bags)

        # Calculate masked attention scores from the embeddings.
        masked_attention_scores = self._all_masked_attention(embeddings, lenses)

        return masked_attention_scores

    # --- Private methods ------------------------------------------------------

    def _verify_input(self, *bags_and_lens):
        """Verify that the input is valid."""
        if len(bags_and_lens) != self.n_input:
            raise ValueError(
                f'Expected {self.n_input} inputs (tuples of bags and lens), got '
                f'{len(bags_and_lens)}'
            )
        for i in range(self.n_input):
            bags, lens = bags_and_lens[i]
            if bags.ndim != 3:
                raise ValueError(f'Bag tensor {i} has {bags.ndim} dimensions, expected 3')
            if bags.shape[0] != lens.shape[0]:
                raise ValueError(
                    f'Bag tensor {i} has {bags.shape[0]} bags, but lens tensor has '
                    f'{lens.shape[0]} entries'
                )

    def _calculate_weighted_embeddings(self, masked_attention_scores, embeddings):
        return [
            getattr(self, f'prehead_{i}')(torch.sum(mas * emb, dim=1))
            for i, (mas, emb) in enumerate(zip(masked_attention_scores, embeddings))
        ]

    def _calculate_embeddings(self, bags):
        """Calculate embeddings for all magnifications."""
        return [
            getattr(self, f'encoder_{i}')(bags[i])
            for i in range(self.n_input)
        ]

    def _all_masked_attention(self, embeddings, lenses):
        """Calculate masked attention scores for all magnification levels."""
        return [
            self._masked_attention_scores(embeddings[i], lenses[i], i)
            for i in range(self.n_input)
        ]

    def _masked_attention_scores(self, embeddings, lens, mag_index):
        """Calculate masked attention scores at the given magnification.

        Returns:
            A tensor containing torch.concat([torch.rand(64, 256), torch.rand(64, 23)], -1)
             *  The attention score of instance i of bag j if i < len[j]
             *  0 otherwise
        """
        bs, bag_size = embeddings.shape[0], embeddings.shape[1]
        attention_scores = getattr(self, f'attention_{mag_index}')(embeddings)

        # a tensor containing a row [0, ..., bag_size-1] for each batch instance
        idx = torch.arange(bag_size).repeat(bs, 1).to(attention_scores.device)

        # False for every instance of bag i with index(instance) >= lens[i]
        attention_mask = (idx < lens.unsqueeze(-1)).unsqueeze(-1)

        masked_attention = torch.where(attention_mask, attention_scores, -torch.inf)
        return torch.softmax(masked_attention, dim=1)

    # --- FastAI compatibility -------------------------------------------------

    def relocate(self):
        """Move model to GPU. Required for FastAI compatibility."""
        self.to(get_device())

    def plot(*args, **kwargs):
        """Override to disable FastAI plotting."""
        pass


class UQ_MultiModal_Attention_MIL(MultiModal_Attention_MIL):
    """Variant of the MultiModal attention-MIL model with uncertainty-weighted fusion."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.head = nn.Sequential(
            nn.Linear(self._z_dim, self._z_dim),
            nn.LayerNorm(self._z_dim),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(self._z_dim, self._n_out)
        )
        self.uq_dropout = nn.Dropout(self._dropout_p)

    def forward(self, *bags_and_lens):
        """Return predictions using all bags and magnifications.

        Input should be a list of tuples, with each tuple containing a bag and
        lens tensors corresponding to a single magnification level. The length
        of the list should be equal to the number of magnification levels.

        """
        self._verify_input(*bags_and_lens)
        bags, lenses = zip(*bags_and_lens)

        embeddings = self._calculate_embeddings(bags)
        masked_attention_scores = self._all_masked_attention(embeddings, lenses)

        merged_embeddings = self._merge_uncertainty_weighted_embeddings(embeddings, masked_attention_scores)

        output = self.head(merged_embeddings)
        return output

    def _merge_uncertainty_weighted_embeddings(self, embeddings, masked_attention_scores):
        weighted_embeddings = self._calculate_weighted_embeddings(masked_attention_scores, embeddings)
        expanded_embeddings = [emb.unsqueeze(1).expand(-1, 30, -1) for emb in weighted_embeddings]
        mode_uncertainty = self._calculate_mode_uncertainty(expanded_embeddings)

        # Weight the embeddings from each magnification by their uncertainty.
        stacked_uncertainty = torch.stack(mode_uncertainty, dim=1)
        uncertainty_weights = 1 - torch.softmax(stacked_uncertainty, dim=1)

        final_weighted_embeddings = self._merge_weighted_embeddings(
            masked_attention_scores, embeddings, uncertainty_weights
        )
        return torch.sum(final_weighted_embeddings, dim=1)

    def _calculate_mode_uncertainty(self, expanded_embeddings):
        mode_uncertainty = []
        for i in range(self.n_input):
            # Enforce dropout.
            _prior_status = self.training
            self.uq_dropout.train()
            dropout_expanded = self.uq_dropout(expanded_embeddings[i])
            self.train(_prior_status)

            # Concatenate the expanded embedding sums with the original
            # embedding sums.
            all_embeddings = [
                    (expanded_embeddings[j] if j!=i else dropout_expanded) * 0.5
                    for j in range(self.n_input)
                ]
            all_embeddings = torch.sum(torch.stack(all_embeddings, dim=2), dim=2)

            # Pass the concatenated embeddings through a final linear layer.
            expanded_scores = self.head(all_embeddings)

            # Average the scores across the 30 dropout samples.
            score_stds = torch.std(expanded_scores, dim=1)
            avg_by_batch = score_stds.mean(axis=1)

            mode_uncertainty.append(avg_by_batch)

        return mode_uncertainty

    def _merge_weighted_embeddings(self, masked_attention_scores, embeddings, uncertainty_weights):
        return torch.stack([
            getattr(self, f'prehead_{i}')(torch.sum(mas * emb, dim=1)) * uncertainty_weights[:, i].unsqueeze(-1)
            for i, (mas, emb) in enumerate(zip(masked_attention_scores, embeddings))
        ], dim=1)
