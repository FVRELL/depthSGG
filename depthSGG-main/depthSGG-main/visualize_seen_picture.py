import h5py
import matplotlib
# matplotlib.use('TkAgg')

import matplotlib.pyplot as plt
# plt.interactive(False)
data=h5py.File('/data1/zhouxukun/SGG-CoRF-main/openpifpaf/data/visual_genome/imdb_512.h5')
print(list(data.keys()))
image=data['images'][0]
image_ids=data['image_ids'][0]
print(image_ids)
plt.imshow(image.transpose(1,2,0)[:,:,::-1])
plt.show()