python train.py \
        --cuda \
        -d coco \
        --root /mnt/share/ssd2/dataset/ \
        -v yolo_s \
        -m basic \
        --ema \
        --fp16 \
        --eval_epoch 10
