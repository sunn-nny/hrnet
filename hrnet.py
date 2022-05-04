import math

import torch
import torch.nn as nn
import torch.nn.functional as F

# BatchNorm的动量
BN_MOMENTUM = 0.2


# 占位符
class PlaceHolder(nn.Module):
    # '''校验完成
    #     占位符，没有什么特别的操作
    #
    #     eg:
    #        model = PlaceHolder()
    #        y = model(tensor)
    #        output:
    #             y == tensor
    # '''
    def __init__(self):
        super(PlaceHolder, self).__init__()

    def forward(self, inputs):
        return inputs


# HRNetConv3x3参数量为812
class HRNetConv3x3(nn.Module):
    # '''校验完成
    #     3x3卷积，进行一般的特征提取操作
    # '''

    def __init__(self, in_channels, out_channels, stride=1, padding=0):
        super(HRNetConv3x3, self).__init__()

        # 关掉bias
        self.conv = nn.Conv2d(in_channels=in_channels, out_channels=out_channels,
                              kernel_size=3, stride=stride, padding=padding, bias=False)
        self.bn = nn.BatchNorm2d(out_channels, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU()

    def forward(self, inputs):
        x = self.conv(inputs)
        x = self.bn(x)
        x = self.relu(x)

        return x


class HRNetStem(nn.Module):
    # ''' 校验完成 -- scale: 1/4.
    #     Stem特征提取输入图像的浅层信息
    #
    #     eg:
    #        model = HRNetStem(3, 64)
    #        y = model(tensor)  # (1, 3, 256, 256)
    #        output:
    #             [1, 64, 64, 64]
    # '''
    def __init__(self, in_channels, out_channels):
        super(HRNetStem, self).__init__()

        self.conv1 = HRNetConv3x3(in_channels=in_channels, out_channels=out_channels, stride=2, padding=1)
        self.conv2 = HRNetConv3x3(in_channels=out_channels, out_channels=out_channels, stride=2, padding=1)

    def forward(self, inputs):
        x = self.conv1(inputs)
        x = self.conv2(x)

        return x


# input layers
# 调整输入通道数使得其匹配输入通道数
class HRNetInput(nn.Module):

    def __init__(self, inchannels, outchannels=64, stage1_inchannels=32):
        super(HRNetInput, self).__init__()

        self.stem = HRNetStem(inchannels, outchannels)

        # stem -> stage1
        self.in_change_conv = nn.Conv2d(outchannels, stage1_inchannels, kernel_size=1,
                                        stride=1, bias=False)
        self.in_chanhe_bn = nn.BatchNorm2d(stage1_inchannels, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU()

    def forward(self, inputs):
        x = self.stem(inputs)  # outchannels == 64
        x = self.in_change_conv(x)  # stage1_inchannels  == 32
        x = self.in_chanhe_bn(x)
        x = self.relu(x)

        return x


# 普通block
class NormalBlock(nn.Module):

    def __init__(self, inchannels, outchannels):
        super(NormalBlock, self).__init__()

        self.conv1 = HRNetConv3x3(in_channels=inchannels, out_channels=outchannels,
                                  stride=1, padding=1)
        self.conv2 = HRNetConv3x3(in_channels=outchannels, out_channels=outchannels,
                                  stride=1, padding=1)

    def forward(self, inputs):
        x = self.conv1(inputs)
        x = self.conv2(x)

        return x


# 残差block
# 残差就是在激活函数之前添加卷积
# 注意所有的HRNetConv3x3都要改in_channels和out_channels
class ResidualBlock(nn.Module):

    def __init__(self, inchannels, outchannels):
        super(ResidualBlock, self).__init__()

        self.conv1 = HRNetConv3x3(in_channels=inchannels, out_channels=outchannels,
                                  stride=1, padding=1)

        self.conv2 = nn.Conv2d(outchannels, outchannels, kernel_size=3,
                               stride=1, padding=1)
        self.bn2 = nn.BatchNorm2d(outchannels, momentum=BN_MOMENTUM)

        self.relu = nn.ReLU()

    def forward(self, inputs):
        residual = inputs

        x = self.conv1(inputs)

        x = self.conv2(x)
        x = self.bn2(x)

        x += residual
        x = self.relu(x)

        return x


class HRNetStage(nn.Module):

    def __init__(self, stage_channels, block):
        super(HRNetStage, self).__init__()

        # stage_channels is list [32, 64, 128]
        self.stage_channels = stage_channels
        self.stage_branch_num = len(stage_channels)
        self.block = block
        self.block_num = 4

        # 得到当前stage_channels满足下的stage网络层
        self.stage_layers = self.create_stage_layers()

    def forward(self, inputs):
        outs = []

        for i in range(len(inputs)):
            x = inputs[i]
            out = self.stage_layers[i](x)
            outs.append(out)

        # [out1, out2]
        return outs

    def create_stage_layers(self):
        tostage_layers = []  # 并行的结构

        # 遍历每一个分支 -- 构建串联的网络结构
        for i in range(self.stage_branch_num):
            branch_layer = []  # 串行的结构
            for j in range(self.block_num):
                branch_layer.append(self.block(self.stage_channels[i], self.stage_channels[i]))
            branch_layer = nn.Sequential(*branch_layer)  # block1, block2
            tostage_layers.append(branch_layer)

        # 当前stage的branch-layer集合
        # LayerList 相当于是一个list
        # LayerList[0] --> out = LayerList[0](x)
        return nn.ModuleList(tostage_layers)


class HRNetTrans(nn.Module):

    def __init__(self, old_branch_channels, new_branch_channels):
        super(HRNetTrans, self).__init__()

        # old_branch_channels is list [branch1, branch2]
        # len([branch1, branch2]) == bracnh_num

        # stages' channels
        self.old_branch_channels = old_branch_channels
        self.new_branch_channels = new_branch_channels

        # branch number
        self.old_branch_num = len(old_branch_channels)
        self.new_branch_num = len(new_branch_channels)

        # 生成——分支拓展以及跨分辨率融合的网络层(LayerList)
        self.trans_layers = self.create_new_branch_trans_layers()

        # type(self.trans_layers)

    def forward(self, inputs):
        # output list
        outs = []

        for i in range(self.old_branch_num):
            x = inputs[i]
            out = []

            # 得到当前第i的输入对应的 self.new_branch_num 个数的输出
            for j in range(self.new_branch_num):
                y = self.trans_layers[i][j](x)
                out.append(y)

            # 2
            # 论文作者这里用到的是concat，代码作者用的是+
            if len(outs) == 0:
                outs = out
            else:
                for i in range(self.new_branch_num):
                    outs[i] += out[i]

        return outs

    def create_new_branch_trans_layers(self):
        # LayerList
        totrans_layers = []  # 所有分支的转换层

        for i in range(self.old_branch_num):
            branch_trans = []
            for j in range(self.new_branch_num):
                layer = []
                inchannels = self.old_branch_channels[i]

                if i == j:
                    layer.append(PlaceHolder())
                elif i < j:
                    # j - i > 0
                    # 1  --> downsample
                    for k in range(j - i):
                        layer.append(
                            nn.Conv2d(in_channels=inchannels,
                                      out_channels=int(inchannels*2),
                                      kernel_size=1, bias=False)
                        )
                        layer.append(
                            nn.BatchNorm2d(int(inchannels*2), momentum=BN_MOMENTUM)
                        )
                        layer.append(
                            nn.ReLU()
                        )
                        # 下采样率: 1/2
                        layer.append(
                            nn.Conv2d(in_channels=int(inchannels*2),
                                      out_channels=int(inchannels*2),
                                      kernel_size=3, stride=2, padding=1, bias=False)
                        )
                        layer.append(
                            nn.BatchNorm2d(int(inchannels*2), momentum=BN_MOMENTUM)
                        )
                        layer.append(
                            nn.ReLU()
                        )
                        # 对输出通道数和分辨率进行调整，为下一次运行做准备
                        inchannels = int(inchannels*2)
                elif i > j:
                    for k in range(i - j):
                        # 应该先上采样还是应该先调整通道数
                        layer.append(
                            nn.Conv2d(in_channels=inchannels,
                                      out_channels=int(inchannels/2),
                                      kernel_size=1, bias=False)
                        )
                        layer.append(
                            nn.BatchNorm2d(int(inchannels/2), momentum=BN_MOMENTUM)
                        )
                        layer.append(
                            nn.ReLU()
                        )
                        # 该部分实现上采样
                        layer.append(
                            nn.Upsample(scale_factor=2.)
                        )
                        inchannels = int(inchannels/2)
                layer = nn.Sequential(*layer)
                branch_trans.append(layer)

            branch_trans = nn.ModuleList(branch_trans)
            totrans_layers.append(branch_trans)

        return nn.ModuleList(totrans_layers)


Fusion_Mode = ['keep', 'fuse', 'multi']


class HRNetFusion(nn.Module):

    def __init__(self, stage4_channels, mode='keep'):
        super(HRNetFusion, self).__init__()

        assert mode in Fusion_Mode, \
            'Please input mode is [keep, fuse, multi], in HRNetFusion.'

        self.stage4_channels = stage4_channels
        self.mode = mode

        # 根据模式去构建融合层
        self.fuse_layer = self.create_fuse_layers()

    def forward(self, inputs):
        x1, x2, x3, x4 = inputs
        outs = []

        if self.mode == Fusion_Mode[0]:
            out = self.fuse_layer(x1)
            outs.append(out)
        elif self.mode == Fusion_Mode[1]:
            out = self.fuse_layer[0](x1)
            out += self.fuse_layer[1](x2)
            out += self.fuse_layer[2](x3)
            out += self.fuse_layer[3](x4)
            outs.append(out)
        elif self.mode == Fusion_Mode[2]:
            out1 = self.fuse_layer[0][0](x1)  # layers
            out1 += self.fuse_layer[0][1](x2)
            out1 += self.fuse_layer[0][2](x3)
            out1 += self.fuse_layer[0][3](x4)
            outs.append(out1)

            out2 = self.fuse_layer[1](out1)
            outs.append(out2)

            out3 = self.fuse_layer[2](out2)
            outs.append(out3)

            out4 = self.fuse_layer[3](out3)
            outs.append(out4)

        return outs

    def create_fuse_layers(self):

        layer = None

        if self.mode == 'keep':
            layer = self.create_keep_fusion_layers()
        elif self.mode == 'fuse':
            layer = self.create_fuse_fusion_layers()
        elif self.mode == 'multi':
            layer = self.create_multi_fusion_layers()

        return layer

    def create_keep_fusion_layers(self):
        self.outchannels = self.stage4_channels[0]
        return PlaceHolder()

    def create_fuse_fusion_layers(self):
        layers = []

        outchannel = self.stage4_channels[3]  # outchannel

        for i in range(0, len(self.stage4_channels)):
            inchannel = self.stage4_channels[i]
            layer = []

            if i != (len(self.stage4_channels) - 1):
                layer.append(nn.Conv2d(in_channels=inchannel, out_channels=outchannel, kernel_size=1, bias=False))
                layer.append(nn.BatchNorm2d(outchannel, momentum=BN_MOMENTUM))
                layer.append(nn.ReLU())

            for j in range(i):
                layer.append(nn.Upsample(scale_factor=2.))

            layers.append(nn.Sequential(*layer))

        self.outchannels = outchannel
        return nn.ModuleList(layers)

    def create_multi_fusion_layers(self):
        multi_fuse_layers = []

        layers = []

        outchannel = self.stage4_channels[3]  # outchannel

        for i in range(len(self.stage4_channels)):
            inchannel = self.stage4_channels[i]
            layer = []

            if i != len(self.stage4_channels) - 1:
                layer.append(nn.Conv2d(in_channels=inchannel, out_channels=outchannel,
                                       kernel_size=1, bias=False))
                layer.append(nn.BatchNorm2d(outchannel, momentum=BN_MOMENTUM))
                layer.append(nn.ReLU())

            for j in range(i):
                layer.append(
                    nn.Upsample(scale_factor=2.)
                )

            layer = nn.Sequential(*layer)
            layers.append(layer)

        # 第一个fuse - layer
        multi_fuse_layers.append(nn.ModuleList(layers))

        # 第二个layer
        multi_fuse_layers.append(
            nn.Sequential(
                nn.Conv2d(outchannel, outchannel, kernel_size=3, stride=2, padding=1, bias=False),
                nn.BatchNorm2d(outchannel, momentum=BN_MOMENTUM),
                nn.ReLU()

            )
        )

        # 第三个layer
        multi_fuse_layers.append(
            nn.Sequential(
                nn.Conv2d(outchannel, outchannel, kernel_size=3, stride=2, padding=1, bias=False),
                nn.BatchNorm2d(outchannel, momentum=BN_MOMENTUM),
                nn.ReLU()
            )
        )

        # 第四个layer
        multi_fuse_layers.append(
            nn.Sequential(
                nn.Conv2d(outchannel, outchannel, kernel_size=3, stride=2, padding=1, bias=False),
                nn.BatchNorm2d(outchannel, momentum=BN_MOMENTUM),
                nn.ReLU()

            )
        )

        self.outchannels = outchannel
        return nn.ModuleList(multi_fuse_layers)


class HRNetOutPut(nn.Module):

    def __init__(self, inchannels, outchannels):
        super(HRNetOutPut, self).__init__()

        self.conv = nn.Conv2d(inchannels, outchannels, kernel_size=1, bias=False)
        self.bn = nn.BatchNorm2d(outchannels, momentum=BN_MOMENTUM)
        self.relu = nn.ReLU()

        self.avg_pool = nn.AdaptiveAvgPool2d(output_size=1)

    def forward(self, inputs):
        N = len(inputs)

        outs = []
        for i in range(N):
            out = self.conv(inputs[i])
            out = self.bn(out)
            out = self.avg_pool(out)
            out = self.relu(out)

            outs.append(out)

        return outs


class HRNetClassification(nn.Module):

    def __init__(self, num_classes):
        super(HRNetClassification, self).__init__()

        self.flatten = nn.Flatten()
        self.fc_out = nn.Linear(2048, num_classes)
        self.sigmoid = nn.Sigmoid()

    def forward(self, inputs):
        outs = []

        for i in range(len(inputs)):
            out = self.flatten(inputs[i])
            out = self.fc_out(out)
            out = self.sigmoid(out)
            outs.append(out)

        return outs


class HRNet(nn.Module):

    def __init__(self, num_classes=2, width=32):
        super(HRNet, self).__init__()

        self.width = width

        if self.width == 16:
            self.stages_channels = [[16], [16, 32], [16, 32, 64], [16, 32, 64, 128]]
        elif self.width == 32:
            self.stages_channels = [[32], [32, 64], [32, 64, 128], [32, 64, 128, 256]]
        elif self.width == 64:
            self.stages_channels = [[64], [64, 128], [64, 128, 256], [64, 128, 256, 512]]

        self.input = HRNetInput(3, outchannels=64, stage1_inchannels=self.width)

        self.stage1 = HRNetStage(self.stages_channels[0], NormalBlock)
        self.trans1 = HRNetTrans(self.stages_channels[0], self.stages_channels[1])

        self.stage2 = HRNetStage(self.stages_channels[1], NormalBlock)
        self.trans2 = HRNetTrans(self.stages_channels[1], self.stages_channels[2])

        self.stage3 = HRNetStage(self.stages_channels[2], NormalBlock)
        self.trans3 = HRNetTrans(self.stages_channels[2], self.stages_channels[3])

        self.stage4 = HRNetStage(self.stages_channels[3], NormalBlock)
        self.fuse_layer = HRNetFusion(self.stages_channels[3], mode='multi')

        self.output = HRNetOutPut(self.fuse_layer.outchannels, 2048)  # 1 2048 64 64/32/16/8

        self.classifier = HRNetClassification(num_classes=num_classes)

    def forward(self, inputs):

        x = self.input(inputs)
        x = [x]

        x = self.stage1(x)
        x = self.trans1(x)  # 2 个 输出 -- 32, 64, 128, 256

        x = self.stage2(x)
        x = self.trans2(x)  # 3 个 输出 -- 32, 64, 128, 256

        x = self.stage3(x)
        x = self.trans3(x)  # 4 个 输出 -- 32, 64, 128, 256

        x = self.stage4(x)  # 4 个 输出 -- 32, 64, 128, 256
        x = self.fuse_layer(x)  # keep : 256, 64*64--32x*32--16*16--8*8

        print(x[-1].shape)  # 512

        x = self.output(x)

        x = self.classifier(x)

        return x[-1]

if __name__ == "__main__":
    from torchsummary import summary

    # 需要使用device来指定网络在GPU还是CPU运行
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    # model = HRNetStage(stage_channels=[32, 64, 128], block=NormalBlock).to(device)
    # model = Testnet()
    model = HRNet(num_classes=2, width=32).to(device)
    # print(model)
    # summary(model, input_size=(3, 256, 256))
    summary(model, (3, 256, 256))
