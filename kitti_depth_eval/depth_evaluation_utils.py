# Mostly based on the code written by Clement Godard:
# https://github.com/mrharicot/monodepth/blob/master/utils/evaluation_utils.py
import numpy as np
from collections import Counter
from path import Path
from scipy.misc import imread
from tqdm import tqdm
from scipy.interpolate import LinearNDInterpolator
import scipy.misc as m


class test_framework_KITTI(object):
    def __init__(self, root, test_files, seq_length=3, min_depth=1e-3, max_depth=100, step=1):
        self.root = root
        self.min_depth, self.max_depth = min_depth, max_depth
        self.calib_dirs, self.gt_files, self.img_files, self.cams = read_scene_data(self.root, test_files, seq_length, step)

    def __getitem__(self, i):
        tgt = imread(self.img_files[i]).astype(np.float32)  # input image
        depth, depth_fill = generate_depth_map(self.calib_dirs[i], self.gt_files[i], tgt.shape[:2], self.cams[i], interp=True)
        # m.imsave('depth_gt.png', depth)
        # m.imsave('depth_gt_filled.png', depth_fill)
        return {'tgt': tgt,
                'path': self.img_files[i],
                'gt_depth': depth,
                'mask': generate_mask(depth, self.min_depth, self.max_depth)
                }

    def __len__(self):
        return len(self.img_files)


###############################################################################
#  EIGEN
#  generate depth ground truths

def read_scene_data(data_root, test_list):
    data_root = Path(data_root)
    gt_files = []
    calib_dirs = []
    im_files = []
    cams = []
    # displacements = []
    # demi_length = (seq_length - 1) // 2
    # shift_range = step * np.arange(-demi_length, demi_length + 1)

    print('getting test metadata ... ')
    for sample in tqdm(test_list):
        tgt_img_path = data_root/sample  # path for image
        # date: '2011_09_26', scence: '2011_09_26_drive_0002_sync', cam_id: 'img_02', index: '0000000069'
        date, scene, cam_id, _, index = sample[:-4].split('/')
        vel_path = data_root/date/scene/'velodyne_points'/'data'/'{}.bin'.format(index[:10])

        if tgt_img_path.isfile():
            gt_files.append(vel_path)
            calib_dirs.append(data_root/date)
            im_files.append(tgt_img_path)
            cams.append(int(cam_id[-2:]))
        else:
            print('{} missing'.format(tgt_img_path))
    return calib_dirs, gt_files, im_files, cams


def load_velodyne_points(file_name):
    # adapted from https://github.com/hunse/kitti
    points = np.fromfile(file_name, dtype=np.float32).reshape(-1, 4)
    points[:,3] = 1
    return points


def lin_interp(shape, xyd):
    # taken from https://github.com/hunse/kitti
    m, n = shape
    ij, d = xyd[:, 1::-1], xyd[:, 2]
    f = LinearNDInterpolator(ij, d, fill_value=0)
    J, I = np.meshgrid(np.arange(n), np.arange(m))
    IJ = np.vstack([I.flatten(), J.flatten()]).T
    disparity = f(IJ).reshape(shape)
    return disparity


def read_calib_file(path):
    # taken from https://github.com/hunse/kitti
    float_chars = set("0123456789.e+- ")
    data = {}
    with open(path, 'r') as f:
        for line in f.readlines():
            key, value = line.split(':', 1)
            value = value.strip()
            data[key] = value
            if float_chars.issuperset(value):
                # try to cast to float array
                try:
                    data[key] = np.array(list(map(float, value.split(' '))))
                except ValueError:
                    # casting error: data[key] already eq. value, so pass
                    pass
    return data


def sub2ind(matrixSize, rowSub, colSub):
    m, n = matrixSize
    return rowSub * (n-1) + colSub - 1


def generate_depth_map(calib_dir, velo_file_name, im_shape, cam=2, interp=False):
    # load calibration files
    cam2cam = read_calib_file(calib_dir/'calib_cam_to_cam.txt')
    velo2cam = read_calib_file(calib_dir/'calib_velo_to_cam.txt')
    velo2cam = np.hstack((velo2cam['R'].reshape(3,3), velo2cam['T'][..., np.newaxis]))
    velo2cam = np.vstack((velo2cam, np.array([0, 0, 0, 1.0])))

    # compute projection matrix velodyne->image plane
    R_cam2rect = np.eye(4)
    R_cam2rect[:3,:3] = cam2cam['R_rect_00'].reshape(3,3)
    P_rect = cam2cam['P_rect_0'+str(cam)].reshape(3,4)
    P_velo2im = np.dot(np.dot(P_rect, R_cam2rect), velo2cam)

    # load velodyne points and remove all behind image plane (approximation)
    # each row of the velodyne data is forward, left, up, reflectance
    velo = load_velodyne_points(velo_file_name)
    velo = velo[velo[:, 0] >= 0, :]

    # project the points to the camera
    velo_pts_im = np.dot(P_velo2im, velo.T).T
    velo_pts_im[:, :2] = velo_pts_im[:,:2] / velo_pts_im[:,-1:]

    # check if in bounds
    # use minus 1 to get the exact same value as KITTI matlab code
    velo_pts_im[:, 0] = np.round(velo_pts_im[:,0]) - 1
    velo_pts_im[:, 1] = np.round(velo_pts_im[:,1]) - 1
    val_inds = (velo_pts_im[:, 0] >= 0) & (velo_pts_im[:, 1] >= 0)
    val_inds = val_inds & (velo_pts_im[:,0] < im_shape[1]) & (velo_pts_im[:,1] < im_shape[0])
    velo_pts_im = velo_pts_im[val_inds, :]

    # project to image
    depth = np.zeros((im_shape))
    depth[velo_pts_im[:, 1].astype(np.int), velo_pts_im[:, 0].astype(np.int)] = velo_pts_im[:, 2]

    # find the duplicate points and choose the closest depth
    inds = sub2ind(depth.shape, velo_pts_im[:, 1], velo_pts_im[:, 0])
    dupe_inds = [item for item, count in Counter(inds).items() if count > 1]
    for dd in dupe_inds:
        pts = np.where(inds == dd)[0]
        x_loc = int(velo_pts_im[pts[0], 0])
        y_loc = int(velo_pts_im[pts[0], 1])
        depth[y_loc, x_loc] = velo_pts_im[pts, 2].min()
    depth[depth < 0] = 0

    if interp:
        # interpolate the depth map to fill in holes
        depth_interp = lin_interp(im_shape, velo_pts_im)
        return depth, depth_interp
    else:
        return depth


def generate_mask(gt_depth, min_depth, max_depth):
    mask = np.logical_and(gt_depth > min_depth,
                          gt_depth < max_depth)
    # crop used by Garg ECCV16 to reprocude Eigen NIPS14 results
    # for that the ground truth is not for the entire image
    gt_height, gt_width = gt_depth.shape
    crop = np.array([0.40810811 * gt_height, 0.99189189 * gt_height,
                     0.03594771 * gt_width,  0.96405229 * gt_width]).astype(np.int32)

    crop_mask = np.zeros(mask.shape)
    crop_mask[crop[0]:crop[1],crop[2]:crop[3]] = 1
    mask = np.logical_and(mask, crop_mask)
    return mask
