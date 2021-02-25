import torch
import torch.nn as nn
import torch.nn.functional as F

import neural_renderer as nr        
import numpy as np

###################################################################
####################### BASIC ARCH COMPONENTS #####################
###################################################################

class GLU(nn.Module):
    """ GLU activation halves the channel number once applied """
    def __init__(self):
        super(GLU, self).__init__()

    def forward(self, x):
        nc = x.size(1)
        assert nc % 2 == 0, 'channels dont divide 2!'
        nc = int(nc/2)
        return x[:, :nc] * torch.sigmoid(x[:, nc:])

def spectral_norm(module, mode=True):
    if mode:
        return nn.utils.spectral_norm(module)

    return module

class UpBlock(nn.Module):
    """ upsample the feature map by a factor of 2x """
    def __init__(self, in_channels, 
                       out_channels, 
                       use_spc_norm=False):
        super(UpBlock, self).__init__()
        
        self.block = nn.Sequential(
              nn.Upsample(scale_factor=2, mode='bilinear'),
              spectral_norm(
                nn.Conv2d(in_channels, 
                          out_channels, 
                          kernel_size=3, 
                          stride=1,
                          padding=1, 
                          bias=False),
                mode=use_spc_norm),
              nn.InstanceNorm2d(out_channels,
                                affine=True, 
                                track_running_stats=False),
              nn.LeakyReLU(inplace=True)
          )
    
    def forward(self, x):
        return self.block(x)

class UpDenseBlock(nn.Module):
    """ upsample the feature map by a factor of 2x """
    def __init__(self, in_channels, 
                       out_channels, 
                       use_spc_norm=False):
        super(UpDenseBlock, self).__init__()
        self.upscale = nn.Upsample(
              scale_factor=2, mode='bilinear')

        self.block = nn.Sequential(
              spectral_norm(
                nn.Conv2d(in_channels, 
                          out_channels, 
                          kernel_size=3, 
                          stride=1,
                          padding=1, 
                          bias=False),
                mode=use_spc_norm),
              nn.InstanceNorm2d(out_channels,
                                affine=True, 
                                track_running_stats=False),
              nn.LeakyReLU(inplace=True)
          )
    
    def forward(self, x):
        x_upscale = self.upscale(x)
        return torch.cat([self.block(x_upscale),
                          x_upscale], dim=1)

class SameBlock(nn.Module):
    """ shape-preserving feature transformation """
    def __init__(self, in_channels, out_channels, r=.01, use_spc_norm=False):
        super(SameBlock, self).__init__()
        
        self.block = nn.Sequential(
              spectral_norm(
                nn.Conv2d(in_channels, 
                          out_channels, 
                          kernel_size=3, 
                          stride=1,
                          padding=1, 
                          bias=False),
                mode=use_spc_norm),
              nn.InstanceNorm2d(out_channels,
                                affine=True, 
                                track_running_stats=False),
              nn.LeakyReLU(r, inplace=True)
          )
    
    def forward(self, x):
        return self.block(x)

class DownBlock(nn.Module):
    """ down-sample the feature map by a factor of 2x """
    def __init__(self, in_channels, out_channels, use_spc_norm=False):
        super(DownBlock, self).__init__()
        
        self.block = nn.Sequential(
              spectral_norm(
                nn.Conv2d(in_channels, 
                        out_channels, 
                        kernel_size=4, 
                        stride=2,
                        padding=1, 
                        bias=False), 
                mode=use_spc_norm),
              nn.InstanceNorm2d(out_channels,
                                affine=True, 
                                track_running_stats=False),
              nn.LeakyReLU(0.2, inplace=True)
          )
    
    def forward(self, x):
        return self.block(x)

class ResBlock(nn.Module):
    def __init__(self, in_channels):
        super(ResBlock, self).__init__()
        self.block = nn.Sequential(
            SameBlock(in_channels, in_channels),            
            nn.Conv2d(in_channels, 
                      in_channels, 
                      kernel_size=3, 
                      stride=1,
                      padding=1, 
                      bias=False),
            nn.InstanceNorm2d(in_channels,
                              affine=True, 
                              track_running_stats=False),
        )


    def forward(self, x):
        return self.block(x) + x

class BaseNetwork(nn.Module):        
    def __init__(self, name):
        super(BaseNetwork, self).__init__()
        self.name = name        

    def init_weights(self, init_type='orthogonal', gain=1.):
        '''
        initialize network's weights
        init_type: normal | xavier | kaiming | orthogonal
        https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix/blob/
        9451e70673400885567d08a9e97ade2524c700d0/models/networks.py#L39
        '''

        def init_func(m):
            classname = m.__class__.__name__
            if hasattr(m, 'weight') and (classname.find('Conv') != -1 \
                or classname.find('Linear') != -1):
                if init_type == 'normal':
                    nn.init.normal_(m.weight.data, 0.0, gain)
                elif init_type == 'xavier':
                    nn.init.xavier_normal_(m.weight.data, gain=gain)
                elif init_type == 'kaiming':
                    nn.init.kaiming_normal_(m.weight.data, a=0, mode='fan_in')
                elif init_type == 'orthogonal':
                    nn.init.orthogonal_(m.weight.data, gain=gain)

                if hasattr(m, 'bias') and m.bias is not None:
                    nn.init.constant_(m.bias.data, 0.0)

            elif classname.find('BatchNorm2d') != -1:
                nn.init.normal_(m.weight.data, 1.0, gain)
                nn.init.constant_(m.bias.data, 0.0)

        self.apply(init_func)

class BaseDecoder(nn.Module):
    def __init__(self, 
                 block_chns=[128, 1024, 512, 256, 128, 64],
                 use_spc_norm=False):
        super(BaseDecoder, self).__init__()
        blocks = []        
        for i in range(0, len(block_chns)-1):
            block = UpBlock(in_channels=block_chns[i], 
                            out_channels=block_chns[i+1],
                            use_spc_norm=use_spc_norm)
            blocks.append(block)        

        self.blocks = nn.Sequential(*blocks)

    def forward(self, x):
        return self.blocks(x)

class BaseDenseDecoder(nn.Module):
    def __init__(self, 
                 block_chns=[128, 1024, 512, 256, 128, 64],
                 use_spc_norm=False):
        super(BaseDenseDecoder, self).__init__()
        blocks = []        
        for i in range(0, 3):
            block = UpBlock(in_channels=block_chns[i], 
                            out_channels=block_chns[i+1],
                            use_spc_norm=use_spc_norm)
            blocks.append(block)        

        block = UpDenseBlock(in_channels=block_chns[3], 
                            out_channels=block_chns[4],
                            use_spc_norm=use_spc_norm)
        blocks.append(block)
        for i in range(4, len(block_chns)-1):
            block = UpDenseBlock(
                            in_channels=block_chns[i]+block_chns[i-1], 
                            out_channels=block_chns[i+1],
                            use_spc_norm=use_spc_norm)
            blocks.append(block)

        self.blocks = nn.Sequential(*blocks)

    def forward(self, x):
        return self.blocks(x)

class BaseEncoder(nn.Module):
    def __init__(self, in_channels, 
                       out_channels, 
                       block_chns=[64, 128, 256, 512, 1024],
                       use_spc_norm=False):
        super(BaseEncoder, self).__init__()
        blocks = [] 
        blocks.append(DownBlock(in_channels=in_channels,
                                out_channels=block_chns[0],
                                use_spc_norm=use_spc_norm))
        for i in range(0, len(block_chns)-1):
            block = DownBlock(in_channels=block_chns[i], 
                              out_channels=block_chns[i+1],
                              use_spc_norm=use_spc_norm)
            blocks.append(block)        
        blocks.append(spectral_norm(
                          nn.Conv2d(in_channels=block_chns[-1],
                                    out_channels=out_channels,
                                    kernel_size=6,
                                    stride=4,
                                    padding=1,
                                    bias=True),
                          mode=use_spc_norm
                          ))
        self.blocks = nn.Sequential(*blocks)

    def forward(self, x):
        return self.blocks(x)

class ImageHead(nn.Module):
    def __init__(self, in_channels, out_channels, use_spc_norm=False):
        super(ImageHead, self).__init__()        
        self.model = nn.Sequential(
            spectral_norm(
              nn.Conv2d(in_channels, 
                      out_channels, 
                      kernel_size=3, 
                      stride=1,
                      padding=1, 
                      bias=True),
              mode=use_spc_norm),            
            nn.Tanh()
        )

    def forward(self, x):
        return self.model(x)

class MaskHead(nn.Module):
    def __init__(self, in_channels, out_channels, use_spc_norm=False):
        super(MaskHead, self).__init__()        
        self.model = nn.Sequential(
            spectral_norm(
              nn.Conv2d(in_channels, 
                      out_channels, 
                      kernel_size=3, 
                      stride=1,
                      padding=1, 
                      bias=True),
              mode=use_spc_norm),            
            nn.Sigmoid()
        )

    def forward(self, x):
        return self.model(x)

class MLP(nn.Module):
    def __init__(self, in_channels,                        
                       out_channels,
                       nef,
                       hi_layers=2,                       
                       use_spc_norm=False):
        super(MLP, self).__init__()
        blocks = [
                 spectral_norm(
                  nn.Linear(in_channels, nef),
                  mode=use_spc_norm
                 )]
        for __ in range(0, hi_layers):
            blocks += [
                       nn.LeakyReLU(0.2, inplace=True),
                       spectral_norm(
                        nn.Linear(nef, nef),
                        mode=use_spc_norm
                      )]
        blocks += [
                  nn.LeakyReLU(0.2, inplace=True),
                  spectral_norm(
                    nn.Linear(nef, out_channels),
                    mode=use_spc_norm
                  )]
        self.model = nn.Sequential(*blocks)  

    def forward(self, x):
        return self.model(x)

class DIMLP(nn.Module):
    def __init__(self, in_channels,                        
                       out_channels,
                       nef,                     
                       use_spc_norm=False):
        super(DIMLP, self).__init__()
        self.h1 = nn.Sequential(
                    spectral_norm(
                      nn.Linear(in_channels, nef),
                      mode=use_spc_norm),
                    nn.LeakyReLU(0.2, inplace=True),
                    spectral_norm(
                      nn.Linear(nef, nef),
                      mode=use_spc_norm)
                    )
        self.h2 = nn.Sequential(
                    spectral_norm(
                      nn.Linear(in_channels, nef),
                      mode=use_spc_norm),
                    nn.LeakyReLU(0.2, inplace=True),
                    spectral_norm(
                      nn.Linear(nef, nef),
                      mode=use_spc_norm)
                    )
        self.out = nn.Sequential(
                    nn.LeakyReLU(0.2, inplace=True),
                    spectral_norm(
                        nn.Linear(nef * 2, nef),
                        mode=use_spc_norm),
                    nn.LeakyReLU(0.2, inplace=True),
                    spectral_norm(
                        nn.Linear(nef, out_channels),
                        mode=use_spc_norm)
                    )
                

    def forward(self, x1, x2):
        h1 = self.h1(x1)
        h2 = self.h2(x2)
        return self.out(torch.cat([h1, h2], dim=1))

class T_Z(nn.Module):
    def __init__(self, z_dim, 
                       Kf_0=2, 
                       Kf_1=4, 
                       Kf_2=5,
                       Kf_3=5,
                       use_spc_norm=False):
        super(T_Z, self).__init__()
        self.enc = BaseEncoder(in_channels=3, 
                               out_channels=z_dim,
                               use_spc_norm=use_spc_norm)
        self.layers_0 = MLP(z_dim,                        
                            Kf_0,
                            nef=200,
                            hi_layers=1,                       
                            use_spc_norm=use_spc_norm)                       
        self.layers_1 = MLP(z_dim,                        
                            Kf_1,
                            nef=200,
                            hi_layers=1,                       
                            use_spc_norm=use_spc_norm)
        self.layers_2 = MLP(z_dim,                        
                            Kf_2,
                            nef=200,
                            hi_layers=1,                       
                            use_spc_norm=use_spc_norm)
        self.layers_3 = MLP(z_dim,                        
                            Kf_3,
                            nef=200,
                            hi_layers=1,                       
                            use_spc_norm=use_spc_norm)
        self.z_dim = z_dim

    def forward(self, x):
        bs = x.size(0)

        z = self.enc(x).view(bs, -1)
        fg_l0 = self.layers_0(z)
        fg_l1 = self.layers_1(z)
        fg_l2 = self.layers_2(z)
        fg_l3 = self.layers_3(z)
        return fg_l0, fg_l1, fg_l2, fg_l3

###################################################################
####################### GENERATOR NETWORKS ########################
###################################################################

class FgNet(BaseNetwork):
    def __init__(self, name='fg_net',
                       z_dim=64, 
                       block_chns=[128, 
                                   1024, 
                                   512, 
                                   256, 
                                   128, 
                                   64],
                       kf=200,
                       im_size=128,
                       use_spc_norm=False,
                       init_weights=True):
        super(FgNet, self).__init__(name=name)

        ############### generator arch ##############
        self.fc = nn.Sequential(
              spectral_norm(
                  nn.Linear(z_dim, block_chns[0] * 4 * 4, bias=True),
                  mode=use_spc_norm),
              nn.LeakyReLU(0.2, inplace=True)
            )

        # object feature base
        # z -> (B, 64, 128, 128)
        self.decode_base = BaseDecoder(block_chns=block_chns,
                                       use_spc_norm=use_spc_norm)

        self.im_head = ImageHead(in_channels=64, 
                                 out_channels=3,
                                 use_spc_norm=use_spc_norm)
        self.ma_head = nn.Sequential(
            spectral_norm(
                nn.Conv2d(in_channels=64, 
                      out_channels=1, 
                      kernel_size=3, 
                      stride=1,
                      padding=1, 
                      bias=True),
                mode=use_spc_norm
              ))        

        ############### encoder arch ##############
        # self.encode_base = T_Z(256, 256, 10, 50)
        if init_weights:
            self.init_weights()        

    def forward(self, z):
        bs = z.size(0)

        z_f = self.fc(z).view(bs, -1, 4, 4)
        obj_latent = self.decode_base(z_f)

        ### image 
        app = self.im_head(obj_latent)

        ### mask
        ma = self.ma_head(obj_latent)        

        ### logits for MI
        app_and_ma = app * ma.sigmoid()
        # f_logits = self.encode_base(app_and_ma)
        
        return app, ma, app_and_ma, z_f

class BgNet(BaseNetwork):
    def __init__(self, name='bg_net',
                       z_dim=32,
                       block_chns=[128, 
                                   1024, 
                                   512, 
                                   256, 
                                   128, 
                                   64],
                       kb=200,
                       use_spc_norm=False,
                       init_weights=True):
        super(BgNet, self).__init__(name=name)

        ############### generator arch ##############
        self.fc = nn.Sequential(
              spectral_norm(
                  nn.Linear(z_dim, block_chns[0] * 4 * 4, bias=True),
                  mode=use_spc_norm),
              nn.LeakyReLU(0.2, inplace=True)
            )

        # object feature base
        # z -> (B, 64, 128, 128)
        self.decode_base = BaseDecoder(block_chns=block_chns,
                                       use_spc_norm=use_spc_norm)

        # final decoding layers
        self.im_head = ImageHead(
                          in_channels=64, 
                          out_channels=3,
                          use_spc_norm=use_spc_norm)
        self.ma_head = nn.Sequential(
            spectral_norm(
                nn.Conv2d(in_channels=64, 
                      out_channels=1, 
                      kernel_size=3, 
                      stride=1,
                      padding=1, 
                      bias=True),
                mode=use_spc_norm
              ))        

        ############### encoder arch ##############
        # self.encode_base = T_Z(256, 256, 10, 50)
        if init_weights:
            self.init_weights()

    def forward(self, z):
        bs = z.size(0)

        z_b = self.fc(z).view(bs, -1, 4, 4)            
        bg_latent = self.decode_base(z_b)

        ### image         
        bg = self.im_head(bg_latent)
        ### mask 
        ma = self.ma_head(bg_latent)

        ### logits for MI        
        # b_logits = self.encode_base(bg * ma.sigmoid())
        return bg, ma, bg * ma.sigmoid(), z_b

class SpNet(BaseNetwork):
    def __init__(self, name='sp_net',
                       z_dim=128, 
                       zf_dim=128,
                       zb_dim=128,
                       im_size=128,     
                       block_chns=[128, 
                                   1024, 
                                   512, 
                                   256, 
                                   128, 
                                   64],
                       use_spc_norm=False,
                       init_weights=True):
        super(SpNet, self).__init__(name=name)
        self.im_size = im_size

        ############### generator arch ##############   
        self.fc = nn.Sequential(
              spectral_norm(
                  nn.Linear(z_dim, block_chns[0] * 4 * 4, bias=True),
                  mode=use_spc_norm),
              nn.LeakyReLU(0.2, inplace=True)
            )        

        # feature base
        # z -> (B, 64, 128, 128)
        block_chns[0] += 128
        self.decode_base_deform = BaseDecoder(
                          block_chns=block_chns,
                          use_spc_norm=use_spc_norm)
        # deform grid est.
        self.decode_deform = ImageHead(64, 2, use_spc_norm)        
        
        if init_weights:
            self.init_weights()

    def forward(self, z, z_f, z_b):
        bs, d_z = z.size()        
 
        ############### bg deform estimation ##############
        z_sp = self.fc(z)
        deform_latent = torch.cat(
            [z_sp.view(bs, -1, 4, 4), z_b],
            dim=1 )
        deform_latent = self.decode_base_deform(deform_latent)        
        deform_grid = self.decode_deform(deform_latent)
        # (B, 2, H, W) -> (B, H, W, 2)
        deform_grid = deform_grid.permute(0, 2, 3, 1)              

        return deform_grid, None, None # s_logits

###################################################################
########################## LATENT EBMS ############################
###################################################################

class CEBMNet(BaseNetwork):
    def __init__(self, name='ebm_net',
                       zf_dim=64,
                       zb_dim=32,
                       zsp_dim=128,
                       nef=200,
                       Kf=200,
                       Kb=200,
                       use_spc_norm=False,
                       init_weights=True):
        super(CEBMNet, self).__init__(name=name)
        self.zf_dim = zf_dim
        self.zb_dim = zb_dim
        self.zsp_dim = zsp_dim

        self.fg_model_0 = MLP(128, 2,                               
                              nef=nef, 
                              hi_layers=1, 
                              use_spc_norm=use_spc_norm)
        self.fg_model_1 = DIMLP(128, 4, 
                              nef=nef,
                              use_spc_norm=use_spc_norm)
        self.fg_model_2 = DIMLP(128, 5,                               
                              nef=nef, 
                              use_spc_norm=use_spc_norm)
        self.fg_model_3 = DIMLP(128, 5, 
                              nef=nef,
                              use_spc_norm=use_spc_norm)


        self.bg_model_0 = MLP(128, 2, 
                              nef=nef,
                              hi_layers=1, 
                              use_spc_norm=use_spc_norm)
        self.bg_model_1 = DIMLP(128, 4, 
                              nef=nef,                              
                              use_spc_norm=use_spc_norm)
        self.bg_model_2 = DIMLP(128, 5, 
                              nef=nef,                              
                              use_spc_norm=use_spc_norm)
        self.bg_model_3 = DIMLP(128, 5, 
                              nef=nef,                              
                              use_spc_norm=use_spc_norm)

        nef *= 2
        self.sp_model = nn.Sequential(
              nn.Linear(zsp_dim, nef),
              nn.LeakyReLU(0.2, inplace=True),
              nn.Linear(nef, nef),
              nn.LeakyReLU(0.2, inplace=True),
              nn.Linear(nef, nef),              
              nn.LeakyReLU(0.2, inplace=True),
              nn.Linear(nef, nef),              
              nn.LeakyReLU(0.2, inplace=True),
              nn.Linear(nef, 1),
            )        

        if init_weights:
            self.init_weights()

    def forward(self, z):
        ##### z = z_fg + z_bg + z_sp
        zf, zb, zs = z[:,:self.zf_dim], \
                     z[:,self.zf_dim:self.zf_dim + self.zb_dim], \
                     z[:,-self.zsp_dim:]

        # zs_cat = torch.cat([zs, zb.detach()], dim=1)
        zs_logits = self.sp_model(zs)

        zf_logits_0 = self.fg_model_0(
            zf[:, :128])
        zf_logits_1 = self.fg_model_1(
            zf[:, :128].detach(), 
            zf[:, 128:256])
        zf_logits_2 = self.fg_model_2(
            zf[:, 128:256].detach(), 
            zf[:, 256:384])
        zf_logits_3 = self.fg_model_3(
            zf[:, 256:384].detach(), 
            zf[:, 384:512])

        zb_logits_0 = self.bg_model_0(
            zb[:, :128])
        zb_logits_1 = self.bg_model_1(
            zb[:, :128].detach(), 
            zb[:, 128:256])
        zb_logits_2 = self.bg_model_2(
            zb[:, 128:256].detach(), 
            zb[:, 256:384])
        zb_logits_3 = self.bg_model_3(
            zb[:, 256:384].detach(), 
            zb[:, 384:512])

        score = torch.logsumexp(zf_logits_0, dim=1, keepdim=True) + \
                torch.logsumexp(zf_logits_1, dim=1, keepdim=True) + \
                torch.logsumexp(zf_logits_2, dim=1, keepdim=True) + \
                torch.logsumexp(zf_logits_3, dim=1, keepdim=True) + \
                torch.logsumexp(zb_logits_0, dim=1, keepdim=True) + \
                torch.logsumexp(zb_logits_1, dim=1, keepdim=True) + \
                torch.logsumexp(zb_logits_2, dim=1, keepdim=True) + \
                torch.logsumexp(zb_logits_3, dim=1, keepdim=True) + \
                zs_logits
        return score, (zf_logits_0, zf_logits_1, zf_logits_2, zf_logits_3), \
                      (zb_logits_0, zb_logits_1, zb_logits_2, zb_logits_3), zs_logits

###################################################################
######################### DISCRIMINATOR ###########################
###################################################################

class DNet_bg(BaseNetwork):
    def __init__(self, 
                 in_channels=3, 
                 name='dnet_bg',
                 init_weights=True):
        super(DNet_bg, self).__init__(name=name)

        self.encode_base = nn.Sequential(
            # 128x128 -> 32x32
            DownBlock(in_channels, 64),
            DownBlock(64, 128),
            # shape-preserving
            SameBlock(128, 256, 0.2),
            SameBlock(256, 512, 0.2),            
        ) 

        self.real_fake_logits = nn.Sequential(            
            nn.Conv2d(
                    in_channels=512, 
                    out_channels=1, 
                    kernel_size=3, 
                    stride=1, 
                    padding=1, 
                    bias=False)
        )

        self.fg_bg_logits = nn.Sequential(            
            nn.Conv2d(
                    in_channels=512, 
                    out_channels=1, 
                    kernel_size=3, 
                    stride=1, 
                    padding=1, 
                    bias=False)
        )

        if init_weights:
            self.init_weights()

    def forward(self, x):
        enc_latent = self.encode_base(x)        
        return self.real_fake_logits(enc_latent), \
               self.fg_bg_logits(enc_latent)

class DNet_all(BaseNetwork):
    def __init__(self, 
                 in_channels=3, 
                 name='dnet_all',
                 init_weights=True):
        super(DNet_all, self).__init__(name=name)

        self.model = nn.Sequential(
            # 128x128 -> 8x8
            DownBlock(in_channels, 64),
            DownBlock(64, 128),
            DownBlock(128, 256),
            DownBlock(256, 512),
            nn.Conv2d(
                    in_channels=512, 
                    out_channels=1, 
                    kernel_size=3, 
                    stride=1, 
                    padding=1, 
                    bias=False)            
        ) 
        
        if init_weights:
            self.init_weights()

    def forward(self, x):
        return self.model(x)                
