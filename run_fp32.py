import os
import sys
import torch
import torchvision
import numpy as np
import cv2
import requests
from tqdm import tqdm
from types import ModuleType
import openvino as ov
from openvino.runtime import Core, properties
import time

if not hasattr(torchvision.transforms, 'functional_tensor'):
    functional_tensor = ModuleType('torchvision.transforms.functional_tensor')
    sys.modules['torchvision.transforms.functional_tensor'] = functional_tensor
    torchvision.transforms.functional_tensor = functional_tensor
    print("✅ 已创建 functional_tensor 兼容模块")


from torchvision.transforms import functional as F


for func_name in ['rgb_to_grayscale', 'adjust_brightness', 'adjust_contrast', 'adjust_saturation']:
    if hasattr(F, func_name) and not hasattr(torchvision.transforms.functional_tensor, func_name):
        setattr(torchvision.transforms.functional_tensor, func_name, getattr(F, func_name))
        print(f"✅ 已添加 {func_name} 到 functional_tensor")

# ========== 模型路径 ==========
MODEL_DIR = "../FP32/models"
os.makedirs(MODEL_DIR, exist_ok=True)
MODEL_NAME = "RealESRGAN_x4plus.pth"
MODEL_PATH = os.path.join(MODEL_DIR, MODEL_NAME)


# ========== 确保模型文件存在 ==========
def download_model():
    """下载模型权重文件"""
    if os.path.exists(MODEL_PATH):
        print(f"✅ 模型文件已存在: {MODEL_PATH}")
        return True

    # 下载链接
    model_url = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"

    print(f"正在下载模型文件: {MODEL_NAME}...")

    try:
        response = requests.get(model_url, stream=True)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        block_size = 1024  # 1KB

        with open(MODEL_PATH, 'wb') as f, tqdm(
                desc=MODEL_NAME,
                total=total_size,
                unit='iB',
                unit_scale=True,
                unit_divisor=1024,
        ) as bar:
            for data in response.iter_content(block_size):
                bar.update(len(data))
                f.write(data)

        print(f"✅ 模型下载完成: {MODEL_PATH}")
        return True
    except Exception as e:
        print(f"❌ 下载失败: {e}")
        return False


# ========== 导出 ONNX 模型 ==========
def export_realesrgan_to_onnx():
    """导出 RealESRGAN 模型为 ONNX 格式"""
    print("初始化 RealESRGAN 模型...")

    try:
        # 显式导入模型架构
        from basicsr.archs.rrdbnet_arch import RRDBNet

        # 创建模型实例
        model = RRDBNet(
            num_in_ch=3,
            num_out_ch=3,
            num_feat=64,
            num_block=23,
            num_grow_ch=32,
            scale=4
        )
        print("✅ 模型架构创建成功")

        # 加载模型权重
        state_dict = torch.load(MODEL_PATH, map_location='cpu')
        print(f"✅ 权重文件加载成功，大小: {len(state_dict)} keys")

        # 识别并加载权重格式
        if 'params_ema' in state_dict:
            print("🔍 检测到 'params_ema' 权重格式")
            model.load_state_dict(state_dict['params_ema'])
        elif 'params' in state_dict:
            print("🔍 检测到 'params' 权重格式")
            model.load_state_dict(state_dict['params'])
        elif 'model' in state_dict:
            print("🔍 检测到 'model' 权重格式")
            model.load_state_dict(state_dict['model'])
        else:
            print("🔍 检测到直接模型权重格式")
            model.load_state_dict(state_dict)

        model.eval()
        print("✅ 权重加载成功")

        # 创建虚拟输入
        dummy_input = torch.randn(1, 3, 64, 64)

        # 导出为 ONNX
        onnx_path = "realesrgan_x4.onnx"
        print(f"导出模型到 {onnx_path}...")

        torch.onnx.export(
            model,
            dummy_input,
            onnx_path,
            opset_version=14,
            input_names=['input'],
            output_names=['output'],
            dynamic_axes={
                'input': {2: 'height', 3: 'width'},
                'output': {2: 'height', 3: 'width'}
            }
        )

        # 验证 ONNX 文件生成
        if os.path.exists(onnx_path):
            print(f"✅ ONNX 导出完成: {onnx_path}")
            return onnx_path
        else:
            print(f"❌ ONNX 文件未生成")
            return None

    except Exception as e:
        print(f"❌ 模型初始化失败: {e}")
        import traceback
        traceback.print_exc()
        return None


# ========== OpenVINO 转换 ==========
def convert_to_openvino(onnx_path):
    """将 ONNX 模型转换为 OpenVINO IR 格式"""
    ov_dir = "ov_model"
    os.makedirs(ov_dir, exist_ok=True)
    model_name = os.path.basename(onnx_path).replace(".onnx", "")
    xml_path = os.path.join(ov_dir, f"{model_name}.xml")

    print(f"正在转换模型: {onnx_path} -> {xml_path}")

    try:
        # 方法1：直接转换并保存
        ov_model = ov.convert_model(onnx_path)
        ov.save_model(ov_model, xml_path, compress_to_fp16=True)

        if os.path.exists(xml_path):
            print(f"✅ 转换成功: {xml_path}")
            return xml_path
        else:
            print("❌ 转换后文件未生成")
            return None

    except Exception as e:
        print(f"❌ 直接转换失败: {e}")
        # 方法2：分步转换
        return convert_step_by_step(onnx_path, xml_path)


def convert_step_by_step(onnx_path, xml_path):
    """分步转换作为备选方案"""
    try:
        print("尝试分步转换...")
        # 1. 读取 ONNX 模型
        core = ov.Core()
        model = core.read_model(onnx_path)

        # 2. 设置动态输入形状
        input_layer = model.input(0)
        partial_shape = input_layer.get_partial_shape()
        if partial_shape[2].is_dynamic and partial_shape[3].is_dynamic:
            print("✅ 模型已支持动态输入")
        else:
            print("⚠️ 设置动态输入形状")
            partial_shape[2] = -1  # 动态高度
            partial_shape[3] = -1  # 动态宽度
            model.reshape({input_layer: partial_shape})

        # 3. 保存为 FP32 格式
        ov.save_model(model, xml_path, compress_to_fp16=False)

        if os.path.exists(xml_path):
            print(f"✅ 分步转换成功: {xml_path}")
            return xml_path
        else:
            print("❌ 分步转换后文件未生成")
            return None

    except Exception as e:
        print(f"❌ 分步转换失败: {e}")
        return None


# ========== GPU 推理 ==========
def run_gpu_inference(ov_model_path, input_image_path, output_image_path):
    """在 Intel GPU (ARC 核显) 上运行推理"""
    try:
        # 初始化 OpenVINO 核心
        core = Core()

        # 记录开始时间
        total_start = time.time()

        # 检查可用设备
        devices = core.available_devices
        print(f"可用设备: {devices}")

        # 强制选择 GPU 设备
        device = "GPU"
        if "GPU" in devices:
            print("✅ 使用 Intel ARC GPU (核显)")
        else:
            print("❌ GPU 不可用，无法继续执行")
            return False

        # 读取模型
        model_read_start = time.time()
        model = core.read_model(ov_model_path)
        model_read_time = time.time() - model_read_start
        print(f"⏱️ 模型读取时间: {model_read_time:.4f} 秒")

        # 准备输入图像
        image_load_start = time.time()
        img = cv2.imread(input_image_path)
        if img is None:
            print(f"❌ 无法读取图像: {input_image_path}")
            # 创建默认图像
            img = np.zeros((256, 256, 3), dtype=np.uint8)
            cv2.putText(img, "GPU Test", (50, 128),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            print("✅ 创建默认测试图像")
        image_load_time = time.time() - image_load_start

        # 调整尺寸为 64 的倍数
        resize_start = time.time()
        h, w = img.shape[:2]
        new_h = (h // 64) * 64
        new_w = (w // 64) * 64

        if new_h != h or new_w != w:
            print(f"调整图像尺寸: {w}x{h} -> {new_w}x{new_h}")
            img = cv2.resize(img, (new_w, new_h))
        resize_time = time.time() - resize_start

        # 预处理
        preprocess_start = time.time()
        input_data = img.astype(np.float32) / 255.0
        input_data = np.transpose(input_data, (2, 0, 1))  
        input_data = np.expand_dims(input_data, axis=0)  
        preprocess_time = time.time() - preprocess_start

        # 获取输入层信息
        input_layer = model.input(0)
        input_layer_name = input_layer.get_any_name()

        # 如果模型支持动态输入，调整形状
        reshape_start = time.time()
        partial_shape = input_layer.get_partial_shape()
        if partial_shape.is_dynamic:
            print("✅ 调整动态输入形状")
            partial_shape[2] = img.shape[0]  # 高度
            partial_shape[3] = img.shape[1]  # 宽度
            model.reshape({input_layer_name: partial_shape})
        reshape_time = time.time() - reshape_start

        # GPU 优化配置
        config_start = time.time()
        config = {
            properties.hint.performance_mode(): properties.hint.PerformanceMode.THROUGHPUT,
            properties.hint.execution_mode(): properties.hint.ExecutionMode.PERFORMANCE,
            properties.enable_profiling(): False
        }
        config_time = time.time() - config_start
        print("✅ 应用 GPU 优化配置")

        # 编译模型（应用配置）
        compile_start = time.time()
        compiled_model = core.compile_model(model, device, config)
        infer_request = compiled_model.create_infer_request()
        compile_time = time.time() - compile_start
        print(f"⏱️ 模型编译时间: {compile_time:.4f} 秒")

        # 推理
        inference_start = time.time()
        infer_request.infer({input_layer_name: input_data})
        inference_time = time.time() - inference_start
        print(f"⏱️ 推理时间: {inference_time:.4f} 秒")

        # 获取结果
        get_results_start = time.time()
        output_layer = compiled_model.output(0)
        result = infer_request.get_output_tensor(output_layer.index).data
        get_results_time = time.time() - get_results_start

        # 后处理
        postprocess_start = time.time()
        output_data = np.squeeze(result, axis=0)
        output_data = np.transpose(output_data, (1, 2, 0))
        output_data = np.clip(output_data * 255, 0, 255).astype(np.uint8)
        postprocess_time = time.time() - postprocess_start

        # 保存结果
        save_start = time.time()
        cv2.imwrite(output_image_path, output_data)
        save_time = time.time() - save_start

        # 计算总时间
        total_time = time.time() - total_start

        # 打印详细时间统计
        print(f"✅ 结果保存至: {output_image_path}")
        print("\n⏱️ 时间统计:")
        print(f"  图像加载: {image_load_time:.4f} 秒")
        print(f"  图像调整: {resize_time:.4f} 秒")
        print(f"  预处理: {preprocess_time:.4f} 秒")
        print(f"  模型调整: {reshape_time:.4f} 秒")
        print(f"  配置设置: {config_time:.4f} 秒")
        print(f"  模型编译: {compile_time:.4f} 秒")
        print(f"  推理执行: {inference_time:.4f} 秒")
        print(f"  结果获取: {get_results_time:.4f} 秒")
        print(f"  后处理: {postprocess_time:.4f} 秒")
        print(f"  结果保存: {save_time:.4f} 秒")
        print(f"---------------------------")
        print(f"  总处理时间: {total_time:.4f} 秒")
        print(f"  平均FPS: {1 / total_time:.2f}" if total_time > 0 else "无法计算FPS")

        return True

    except Exception as e:
        print(f"❌ 推理失败: {e}")
        import traceback
        traceback.print_exc()
        return False


# ========== 主函数 ==========
def main():
    print("=" * 50)
    print("Intel GPU (ARC) 超分辨率演示")
    print("=" * 50)
    # 记录总开始时间
    program_start = time.time()
    # 1. 确保模型文件存在
    if not download_model():
        print("❌ 模型下载失败，程序终止")
        return

    # 2. 导出 ONNX 模型
    onnx_path = export_realesrgan_to_onnx()
    if onnx_path is None or not os.path.exists(onnx_path):
        print("❌ ONNX 导出失败，程序终止")
        return

    # 3. 转换为 OpenVINO 格式
    ov_path = convert_to_openvino(onnx_path)
    if ov_path is None or not os.path.exists(ov_path):
        print("❌ OpenVINO 转换失败，程序终止")
        return

    # 4. 准备输入图像
    input_image = "input.jpg"
    output_image = "output.jpg"  # 修改输出文件名

    # 5. 在 GPU 上运行推理
    print(f"\n{'=' * 50}")
    print(f"在 GPU 上处理图像: {input_image}")
    print(f"输出将保存至: {output_image}")
    print("=" * 50)

    inference_start = time.time()
    success = run_gpu_inference(ov_path, input_image, output_image)
    inference_time = time.time() - inference_start

    # 计算总程序时间
    total_program_time = time.time() - program_start
    if success:
        print("\n🎉 所有步骤完成！")
        print(f"原始图像: {input_image}")
        print(f"超分辨率结果: {output_image}")
        print(f"总推理时间: {inference_time:.4f} 秒")
        print(f"总程序运行时间: {total_program_time:.4f} 秒")

        # 显示结果（如果可能）
        try:
            import matplotlib.pyplot as plt
            fig, axs = plt.subplots(1, 2, figsize=(12, 6))

            # 原始图像
            orig_img = cv2.imread(input_image)
            if orig_img is None:
                print(f"⚠️ 无法读取原始图像: {input_image}")
                orig_img = np.zeros((256, 256, 3), dtype=np.uint8)
                cv2.putText(orig_img, "Missing", (50, 128),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
            else:
                orig_img = cv2.cvtColor(orig_img, cv2.COLOR_BGR2RGB)

            axs[0].imshow(orig_img)
            axs[0].set_title('原始图像')
            axs[0].axis('off')

            # 超分辨率结果
            result_img = cv2.imread(output_image)
            if result_img is None:
                print(f"⚠️ 无法读取结果图像: {output_image}")
                result_img = np.zeros((256, 256, 3), dtype=np.uint8)
                cv2.putText(result_img, "Missing", (50, 128),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
            else:
                result_img = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB)

            axs[1].imshow(result_img)
            axs[1].set_title('超分辨率结果')
            axs[1].axis('off')

        except ImportError:
            print("⚠️ Matplotlib 未安装，跳过图像对比")
    else:
        print("❌ 推理过程失败")


if __name__ == "__main__":
    # 添加 OpenVINO 到 PATH（Windows 可能需要）
    try:
        import openvino

        ov_path = os.path.dirname(openvino.__file__)
        scripts_path = os.path.join(ov_path, "..", "..", "Scripts")
        if os.path.exists(scripts_path) and scripts_path not in os.environ["PATH"]:
            os.environ["PATH"] += os.pathsep + os.path.abspath(scripts_path)
            print(f"✅ 添加 OpenVINO 到 PATH: {scripts_path}")
    except ImportError:
        print("❌ OpenVINO 未安装")
        # 尝试安装
        try:
            import pip

            pip.main(['install', 'openvino-dev[onnx]==2023.3.0'])
            print("✅ 已安装 OpenVINO")
        except:
            print("❌ 无法自动安装 OpenVINO，请手动安装")

    main()
