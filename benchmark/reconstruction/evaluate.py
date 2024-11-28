# Copyright (c) 2023, ETH Zurich and UNC Chapel Hill.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
#     * Neither the name of ETH Zurich and UNC Chapel Hill nor the names of
#       its contributors may be used to endorse or promote products derived
#       from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDERS OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import argparse
import collections
import copy
import datetime
import shutil
import subprocess
from pathlib import Path

import numpy as np

import pycolmap


def colmap_reconstruction(
    args,
    workspace_path,
    image_path,
    camera_prior_sparse_gt=None,
    extra_args=None,
):
    workspace_path.mkdir(parents=True, exist_ok=True)

    database_path = workspace_path / "database.db"
    if args.overwrite_database and database_path.exists():
        database_path.unlink()

    sparse_path = workspace_path / "sparse"
    if args.overwrite_reconstruction and sparse_path.exists():
        shutil.rmtree(sparse_path)

    if sparse_path.exists():
        print("Skipping reconstruction, as it already exists")
        return

    args = [
        args.colmap_path,
        "automatic_reconstructor",
        "--image_path",
        image_path,
        "--workspace_path",
        workspace_path,
        "--vocab_tree_path",
        args.data_path / "vocab_tree_flickr100K_words256K.bin",
        "--use_gpu",
        "1" if args.use_gpu else "0",
        "--num_threads",
        str(args.num_threads),
        "--quality",
        args.quality,
    ]

    subprocess.check_call(
        args
        + (extra_args or [])
        + [
            "--extraction",
            "1",
            "--matching",
            "0",
            "--sparse",
            "0",
            "--dense",
            "0",
        ],
        cwd=workspace_path,
    )

    if camera_prior_sparse_gt is not None:
        print("Setting prior cameras from GT")

        database = pycolmap.Database()
        database.open(database_path)

        camera_id_gt_to_camera_id = {}
        for camera_id_gt, camera_gt in camera_prior_sparse_gt.cameras.items():
            camera_gt.has_prior_focal_length = True
            camera_id = database.write_camera(camera_gt)
            camera_id_gt_to_camera_id[camera_id_gt] = camera_id

        images_gt_by_name = {}
        for image_gt in camera_prior_sparse_gt.images.values():
            images_gt_by_name[image_gt.name] = image_gt

        for image in database.read_all_images():
            if image.name not in images_gt_by_name:
                print(
                    f"Not setting prior camera for image {image.name}, "
                    "because it does not exist in GT"
                )
                continue
            image_gt = images_gt_by_name[image.name]
            camera_id = camera_id_gt_to_camera_id[image_gt.camera_id]
            image.camera_id = camera_id
            database.update_image(image)

        database.close()

    subprocess.check_call(
        args
        + (extra_args or [])
        + [
            "--extraction",
            "0",
            "--matching",
            "1",
            "--sparse",
            "1",
            "--dense",
            "0",
        ],
        cwd=workspace_path,
    )


def colmap_alignment(
    args, sparse_path, sparse_gt_path, sparse_aligned_path, max_ref_model_error
):
    if args.overwrite_alignment and sparse_aligned_path.exists():
        shutil.rmtree(sparse_aligned_path)
    if sparse_aligned_path.exists():
        print("Skipping alignment, as it already exists")
        return

    if sparse_path.exists():
        sparse_aligned_path.mkdir(parents=True, exist_ok=True)
        subprocess.call(
            [
                args.colmap_path,
                "model_aligner",
                "--input_path",
                sparse_path,
                "--ref_model_path",
                sparse_gt_path,
                "--output_path",
                sparse_aligned_path,
                "--alignment_max_error",
                str(max_ref_model_error),
            ]
        )


SceneInfo = collections.namedtuple(
    "SceneInfo",
    [
        "category",
        "scene",
        "workspace_path",
        "image_path",
        "sparse_gt_path",
        "sparse_gt",
        "position_accuracy_gt",
        "colmap_extra_args",
    ],
)


def reconstruct_scene(args, scene_info):
    colmap_reconstruction(
        args=args,
        workspace_path=scene_info.workspace_path,
        image_path=scene_info.image_path,
        camera_prior_sparse_gt=scene_info.sparse_gt,
        extra_args=scene_info.colmap_extra_args,
    )

    sparse_path = scene_info.workspace_path / "sparse/0"
    if args.error_type == "relative":
        dts, dRs = compute_rel_errors(
            sparse_gt=scene_info.sparse_gt,
            sparse=pycolmap.Reconstruction(sparse_path),
            min_proj_center_dist=scene_info.position_accuracy_gt,
        )
        errors = [max(dt, dR) for dt, dR in zip(dts, dRs)]
    elif args.error_type == "absolute":
        sparse_aligned_path = scene_info.workspace_path / "sparse_aligned"
        colmap_alignment(
            args=args,
            sparse_path=sparse_path,
            sparse_gt_path=scene_info.sparse_gt_path,
            sparse_aligned_path=sparse_aligned_path,
            max_ref_model_error=scene_info.position_accuracy_gt,
        )
        sparse_aligned = (
            pycolmap.Reconstruction(sparse_aligned_path)
            if (sparse_aligned_path / "images.bin").exists()
            else None
        )
        dts, dRs = compute_abs_errors(
            sparse_gt=scene_info.sparse_gt,
            sparse=sparse_aligned,
        )
        errors = dts
    else:
        raise ValueError(f"Invalid error type: {args.error_type}")

    return errors


def process_scenes(args, scene_infos, error_thresholds, position_accuracy_gt):
    results = collections.defaultdict(dict)
    errors_by_category = collections.defaultdict(list)

    for scene_info in scene_infos:
        errors = reconstruct_scene(args, scene_info)

        errors_by_category[scene_info.category].extend(errors)
        results[scene_info.category][scene_info.scene] = compute_auc(
            errors,
            error_thresholds,
            min_error=position_accuracy_gt,
        )

    for category, errors in errors_by_category.items():
        results[category]["__all__"] = compute_auc(
            errors,
            error_thresholds,
            min_error=position_accuracy_gt,
        )
        results[category]["__avg__"] = compute_avg_auc(results[category])

    return results


def normalize_vec(vec, eps=1e-10):
    return vec / max(eps, np.linalg.norm(vec))


def rot_mat_angular_dist_deg(rot_mat1, rot_mat2):
    cos_dist = np.clip(((np.trace(rot_mat1 @ rot_mat2.T)) - 1) / 2, -1, 1)
    return np.rad2deg(np.acos(cos_dist))


def vec_angular_dist_deg(vec1, vec2):
    cos_dist = np.clip(np.dot(normalize_vec(vec1), normalize_vec(vec2)), -1, 1)
    return np.rad2deg(np.acos(cos_dist))


def get_error_thresholds(args):
    if args.error_type == "relative":
        return args.rel_error_thresholds
    elif args.error_type == "absolute":
        return [100 * t for t in args.abs_error_thresholds]
    else:
        raise ValueError(f"Invalid error type: {args.error_type}")


def compute_rel_errors(sparse_gt, sparse, min_proj_center_dist):
    """Computes angular relative pose errors across all image pairs.

    Notice that this approach leads to a super-linear decrease in the AUC scores
    when multiple images fails to register. Consider that we have N images in
    total in a dataset and M images are registered in the evaluated
    reconstruction. In this case, we can compute "finite" errors for (N-M)^2
    pairs while the dataset has a total of N^2 pairs. In case of many
    unregistered images, the AUC score will drop much more than the
    (intuitively) expected (N-M) / N ratio. One could appropriately normalize by
    computing a single score per image through a suitable normalization of all
    pairwise errors per image. However, this becomes difficult when multiple
    sub-components are incorrectly stitched together in the same reconstruction
    (e.g., in the case of symmetry issues).
    """

    if sparse is None:
        print("Reconstruction failed")
        return len(sparse_gt.images) * [np.inf], len(sparse_gt.images) * [180]

    images = {}
    for image in sparse.images.values():
        images[image.name] = image

    dts = []
    dRs = []
    for this_image_gt in sparse_gt.images.values():
        if this_image_gt.name not in images:
            for _ in range(sparse_gt.num_images() - 1):
                dts.append(np.inf)
                dRs.append(180)
            continue

        this_image = images[this_image_gt.name]

        for other_image_gt in sparse_gt.images.values():
            if this_image_gt.image_id == other_image_gt.image_id:
                continue

            if other_image_gt.name not in images:
                dts.append(np.inf)
                dRs.append(180)
                continue

            other_image = images[other_image_gt.name]

            this_from_other = (
                this_image.cam_from_world * other_image.cam_from_world.inverse()
            )
            this_from_other_gt = (
                this_image_gt.cam_from_world
                * other_image_gt.cam_from_world.inverse()
            )

            proj_center_dist_gt = np.linalg.norm(
                this_image_gt.projection_center()
                - other_image_gt.projection_center()
            )
            if proj_center_dist_gt < min_proj_center_dist:
                # If the cameras almost coincide, then the angular direction
                # distance is unstable, because a small position change can
                # cause a large rotational error. In this case, we only measure
                # rotational relative pose error.
                dt = 0
            else:
                dt = vec_angular_dist_deg(
                    this_from_other.translation, this_from_other_gt.translation
                )

            dR = rot_mat_angular_dist_deg(
                this_from_other.rotation.matrix(),
                this_from_other_gt.rotation.matrix(),
            )

            dts.append(dt)
            dRs.append(dR)

    return dts, dRs


def compute_abs_errors(sparse_gt, sparse):
    if sparse is None:
        print("Reconstruction or alignment failed")
        return len(sparse_gt.images) * [np.inf], len(sparse_gt.images) * [180]

    images = {}
    for image in sparse.images.values():
        images[image.name] = image

    dts = []
    dRs = []
    for image_gt in sparse_gt.images.values():
        if image_gt.name not in images:
            dts.append(np.inf)
            dRs.append(180)
            continue

        image = images[image_gt.name]

        dt = np.linalg.norm(
            image_gt.projection_center() - image.projection_center()
        )
        dR = rot_mat_angular_dist_deg(
            image_gt.cam_from_world.rotation.matrix(),
            image.cam_from_world.rotation.matrix(),
        )

        dts.append(dt)
        dRs.append(dR)

    return dts, dRs


def compute_recall(errors):
    num_elements = len(errors)
    errors = np.sort(errors)
    recall = (np.arange(num_elements) + 1) / num_elements
    return errors, recall


def compute_auc(errors, thresholds, min_error=None):
    if len(errors) == 0:
        raise ValueError("No errors to evaluate")

    errors, recall = compute_recall(errors)

    if min_error is not None:
        min_index = np.searchsorted(errors, min_error, side="right")
        min_score = min_index / len(errors)
        recall = np.r_[min_score, min_score, recall[min_index:]]
        errors = np.r_[0, min_error, errors[min_index:]]
    else:
        recall = np.r_[0, recall]
        errors = np.r_[0, errors]

    aucs = []
    for t in thresholds:
        last_index = np.searchsorted(errors, t, side="right")
        r = np.r_[recall[:last_index], recall[last_index - 1]]
        e = np.r_[errors[:last_index], t]
        auc = np.trapezoid(r, x=e) / t
        aucs.append(auc * 100)
    return aucs


def compute_avg_auc(scene_aucs):
    auc_sum = None
    num_scenes = 0
    for scene, aucs in scene_aucs.items():
        if scene.startswith("__") and scene.endswith("__"):
            continue
        num_scenes += 1
        if auc_sum is None:
            auc_sum = copy.copy(aucs)
        else:
            for i in range(len(auc_sum)):
                auc_sum[i] += aucs[i]
    return [auc / num_scenes for auc in auc_sum]


def evaluate_eth3d(args, position_accuracy_gt=0.001):
    error_thresholds = get_error_thresholds(args)

    scene_infos = []
    for category_path in (args.data_path / "eth3d").iterdir():
        if not category_path.is_dir() or (
            args.categories and category_path.name not in args.categories
        ):
            continue

        category = category_path.name

        for scene_path in sorted(category_path.iterdir()):
            if not scene_path.is_dir():
                continue

            scene = scene_path.name
            if args.scenes and scene not in args.scenes:
                continue

            workspace_path = (
                args.run_path / args.run_name / "eth3d" / category / scene
            )
            image_path = scene_path / "images"
            sparse_gt_path = list(scene_path.glob("*_calibration_undistorted"))[
                0
            ]
            sparse_gt = pycolmap.Reconstruction(sparse_gt_path)

            print(f"Processing ETH3D: category={category}, scene={scene}")

            colmap_extra_args = []
            if category == "dslr":
                colmap_extra_args.extend(["--data_type", "individual"])
            elif category == "rig":
                colmap_extra_args.extend(["--data_type", "video"])

            scene_info = SceneInfo(
                category=category,
                scene=scene,
                workspace_path=workspace_path,
                image_path=image_path,
                sparse_gt_path=sparse_gt_path,
                sparse_gt=sparse_gt,
                position_accuracy_gt=position_accuracy_gt,
                colmap_extra_args=colmap_extra_args,
            )

            scene_infos.append(scene_info)

    results = process_scenes(
        args, scene_infos, error_thresholds, position_accuracy_gt
    )

    return results


def evaluate_imc(args, year, position_accuracy_gt=0.02):
    folder_name = f"imc{year}"

    error_thresholds = get_error_thresholds(args)

    scene_infos = []
    for category_path in Path(
        args.data_path / f"{folder_name}/train"
    ).iterdir():
        if not category_path.is_dir() or (
            args.categories and category_path.name not in args.categories
        ):
            continue

        category = category_path.name

        for scene_path in category_path.iterdir():
            if not scene_path.is_dir():
                continue

            scene = scene_path.name
            if args.scenes and scene not in args.scenes:
                continue

            workspace_path = (
                args.run_path / args.run_name / folder_name / category / scene
            )
            image_path = scene_path / "images"
            train_image_names = set(
                image.name for image in image_path.iterdir()
            )
            sparse_gt_path = scene_path / "sfm"
            sparse_gt = pycolmap.Reconstruction(sparse_gt_path)
            hold_out_image_ids = [
                image.image_id
                for image in sparse_gt.images.values()
                if image.name not in train_image_names
            ]
            for image_id in hold_out_image_ids:
                del sparse_gt.images[image_id]

            print(f"Processing IMC {year}: category={category}, scene={scene}")

            scene_info = SceneInfo(
                category=category,
                scene=scene,
                workspace_path=workspace_path,
                image_path=image_path,
                sparse_gt_path=sparse_gt_path,
                sparse_gt=sparse_gt,
                position_accuracy_gt=position_accuracy_gt,
                colmap_extra_args=None,
            )

            scene_infos.append(scene_info)

    results = process_scenes(
        args, scene_infos, error_thresholds, position_accuracy_gt
    )

    return results


def format_results(args, results):
    if args.error_type == "relative":
        metric = "AUC @ X deg (%)"
        thresholds = args.rel_error_thresholds
    elif args.error_type == "absolute":
        metric = "AUC @ X cm (%)"
        thresholds = [100 * t for t in args.abs_error_thresholds]
    else:
        raise ValueError(f"Invalid error type: {args.error_type}")

    column = "scenes"
    size1 = max(
        len(column) + 2,
        max(len(s) for d in results.values() for c in d.values() for s in c),
    )
    size2 = max(len(metric) + 2, len(thresholds) * 7 - 1)
    header = f"{column:=^{size1}} {metric:=^{size2}}"
    header += "\n" + " " * (size1 + 1)
    header += " ".join(f'{str(t).rstrip("."):^6}' for t in thresholds)
    text = [header]
    for dataset, category_results in results.items():
        for category, scene_results in category_results.items():
            text.append(f"\n{dataset + '=' + category:=^{size1 + size2 + 1}}")
            for scene, aucs in scene_results.items():
                assert len(aucs) == len(thresholds)
                row = ""
                if scene == "__avg__":
                    scene = "average"
                    row += "-" * (size1 + size2 + 1) + "\n"
                if scene == "__all__":
                    scene = "overall"
                    row += "-" * (size1 + size2 + 1) + "\n"
                row += f"{scene:<{size1}} "
                row += " ".join(f"{auc:>6.2f}" for auc in aucs)
                text.append(row)
    return "\n".join(text)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data_path", default=Path(__file__).parent / "data", type=Path
    )
    parser.add_argument(
        "--datasets", nargs="+", default=["eth3d", "imc2023", "imc2024"]
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=[],
        help="Categories to evaluate, if empty all categories are evaluated.",
    )
    parser.add_argument(
        "--scenes",
        nargs="+",
        default=[],
        help="Scenes to evaluate, if empty all scenes are evaluated.",
    )
    parser.add_argument(
        "--run_path", default=Path(__file__).parent / "runs", type=Path
    )
    parser.add_argument(
        "--run_name",
        default=datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
    )
    parser.add_argument(
        "--overwrite_database", default=False, action="store_true"
    )
    parser.add_argument(
        "--overwrite_reconstruction", default=False, action="store_true"
    )
    parser.add_argument(
        "--overwrite_alignment", default=False, action="store_true"
    )
    parser.add_argument("--colmap_path", required=True)
    parser.add_argument("--use_gpu", default=True, action="store_true")
    parser.add_argument("--use_cpu", dest="use_gpu", action="store_false")
    parser.add_argument("--num_threads", type=int, default=-1)
    parser.add_argument("--quality", default="high")
    parser.add_argument(
        "--error_type",
        default="relative",
        choices=["relative", "absolute"],
        help="Whether to evaluate relative pairwise pose errors in angular "
        "distance or absolute pose errors through GT alignment.",
    )
    parser.add_argument(
        "--rel_error_thresholds",
        type=float,
        nargs="+",
        default=[0.5, 1, 5, 10],
        help="Evaluation thresholds in degrees.",
    )
    parser.add_argument(
        "--abs_error_thresholds",
        type=float,
        nargs="+",
        default=[0.02, 0.05, 0.2, 0.5],
        help="Evaluation thresholds in meters.",
    )
    args = parser.parse_args()
    args.colmap_path = Path(args.colmap_path).resolve()
    if args.overwrite_database:
        print("Overwriting database also overwrites reconstruction")
        args.overwrite_reconstruction = True
    if args.overwrite_reconstruction:
        print("Overwriting reconstruction also overwrites alignment")
        args.overwrite_alignment = True
    return args


def main():
    args = parse_args()

    results = {}
    if "eth3d" in args.datasets:
        results["eth3d"] = evaluate_eth3d(args)
    if "imc2023" in args.datasets:
        results["imc2023"] = evaluate_imc(args, year=2023)
    if "imc2024" in args.datasets:
        results["imc2024"] = evaluate_imc(args, year=2024)

    print("\nResults\n")
    print(format_results(args, results))


if __name__ == "__main__":
    main()
