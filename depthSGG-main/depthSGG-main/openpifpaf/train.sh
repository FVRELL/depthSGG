CUDA_VISIBLE_DEVICES=0,1 python -m openpifpaf.train  --lr=5e-4 --lr-basenet=2e-5 --b-scale=10.0 --lr-raf=6e-4 \
--epochs=60 --lr-decay 10 40 50 --batch-size=32 --weight-decay=5e-5 --basenet=resnet50 \
--resnet-pool0-stride=2 --resnet-block5-dilation=2 --vg-cn-upsample 1 --dataset vg --vg-cn-square-edge 512 \
--vg-cn-use-512 --vg-cn-group-deform --vg-cn-single-supervision --cf3-deform-deform4-head \
--cntrnet-deform-deform4-head  --adamw  --output train_res50_rgb_deformer_v2/model

