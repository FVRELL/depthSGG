CUDA_VISIBLE_DEVICES=2,3 python3 -m openpifpaf.eval_cn \
  --checkpoint train_res50_rgb_with_transfomer/model.epoch060 \
  --loader-workers=2 \
  --resnet-pool0-stride=2 --resnet-block5-dilation=2 \
  --dataset vg --decoder cifdetraf_cn --vg-cn-use-512  --vg-cn-group-deform --vg-cn-single-supervision --run-metric \
  --vg-cn-single-head --cf3-deform-use-transformer --cf3-deform-deform4-head --cntrnet-deform-deform4-head
