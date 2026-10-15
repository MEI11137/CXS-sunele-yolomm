<!-- MD5: 6d2eb6ac0c2939b212a87ac106264071 -->
<!--
版权所有©2025 羊羊小栈(GJQ) | 作者: 羊羊小栈 | 时间: 2025-09-06 23:44:27

本系统为原创作品,禁止二次销售!禁止将系统对应的视频、文档进行二次发布!
违者需立即停止侵权行为,并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿,并承担相应法律责任。
-->

<template>
  <div class="training-chart">
    <div class="chart-header">
      <h4>{{ title }}</h4>
      <el-select 
        v-model="selectedMetrics" 
        multiple 
        collapse-tags 
        collapse-tags-tooltip
        placeholder="选择指标"
        style="width: 300px"
        @change="updateChart"
      >
        <el-option
          v-for="metric in availableMetrics"
          :key="metric"
          :label="formatMetricName(metric)"
          :value="metric"
        />
      </el-select>
    </div>
    <div ref="chartRef" class="chart-container"></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import * as echarts from 'echarts'

interface Props {
  data: Array<{[key: string]: number | string}>
  title?: string
  height?: string
}

const props = withDefaults(defineProps<Props>(), {
  title: '训练指标',
  height: '400px'
})

const chartRef = ref<HTMLDivElement>()
const selectedMetrics = ref<string[]>([])
let chartInstance: echarts.ECharts | null = null

// 提取可用的数值指标
const availableMetrics = ref<string[]>([])

// 格式化指标名称显示
const formatMetricName = (metric: string) => {
  const nameMap: {[key: string]: string} = {
    'train/box_loss': '训练框损失',
    'train/cls_loss': '训练分类损失', 
    'train/dfl_loss': '训练DFL损失',
    'val/box_loss': '验证框损失',
    'val/cls_loss': '验证分类损失',
    'val/dfl_loss': '验证DFL损失',
    'metrics/precision(B)': '精确度',
    'metrics/recall(B)': '召回率',
    'metrics/mAP50(B)': 'mAP@0.5',
    'metrics/mAP50-95(B)': 'mAP@0.5:0.95',
    'lr/pg0': '学习率PG0',
    'lr/pg1': '学习率PG1',
    'lr/pg2': '学习率PG2'
  }
  return nameMap[metric] || metric
}

// 初始化图表
const initChart = () => {
  if (!chartRef.value) return
  
  // 如果图表实例已存在，先销毁
  if (chartInstance) {
    chartInstance.dispose()
  }
  
  chartInstance = echarts.init(chartRef.value)
  
  // 从数据中提取数值指标
  if (props.data && props.data.length > 0) {
    const firstRow = props.data[0]
    availableMetrics.value = Object.keys(firstRow).filter(key => {
      const value = firstRow[key]
      return key !== 'epoch' && typeof value === 'number'
    })
    
    // 默认选择前4个指标
    selectedMetrics.value = availableMetrics.value.slice(0, 4)
  }
  
  updateChart()
  
  // 确保图表正确渲染
  setTimeout(() => {
    if (chartInstance) {
      chartInstance.resize()
    }
  }, 100)
}

// 更新图表
const updateChart = () => {
  if (!chartInstance || !props.data || props.data.length === 0) return
  
  // 提取epoch数据作为x轴
  const epochs = props.data.map(item => item.epoch || 0)
  
  // 为每个选中的指标创建系列
  const series = selectedMetrics.value.map(metric => ({
    name: formatMetricName(metric),
    type: 'line',
    smooth: true,
    symbol: 'circle',
    symbolSize: 4,
    data: props.data.map(item => Number(item[metric]) || 0)
  }))
  
  const option = {
    title: {
      text: props.title,
      left: 'center',
      textStyle: {
        fontSize: 16,
        fontWeight: 'normal'
      }
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross'
      },
      formatter: function(params: any) {
        let result = `Epoch: ${params[0].axisValue}<br/>`
        params.forEach((param: any) => {
          result += `${param.seriesName}: ${param.value.toFixed(6)}<br/>`
        })
        return result
      }
    },
    legend: {
      top: 30,
      type: 'scroll',
      data: series.map(s => s.name) // 明确指定图例数据
    },
    grid: {
      top: 80,
      left: '3%',
      right: '4%',
      bottom: '10%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: epochs,
      name: 'Epoch',
      nameLocation: 'middle',
      nameGap: 25
    },
    yAxis: {
      type: 'value',
      scale: true,
      name: '指标值',
      nameLocation: 'middle',
      nameGap: 40
    },
    series: series,
    dataZoom: [
      {
        type: 'inside',
        start: 0,
        end: 100
      },
      {
        type: 'slider',
        start: 0,
        end: 100,
        height: 20,
        bottom: 10
      }
    ]
  }
  
  // 使用 notMerge: true 确保图表完全重新渲染，包括图例
  chartInstance.setOption(option, true)
}

// 监听数据变化
watch(() => props.data, () => {
  if (chartInstance) {
    initChart()
  }
}, { deep: true })

// 监听窗口大小变化和容器变化
const handleResize = () => {
  if (chartInstance) {
    chartInstance.resize()
  }
}

// 使用ResizeObserver监听容器大小变化
let resizeObserver: ResizeObserver | null = null

onMounted(() => {
  initChart()
  window.addEventListener('resize', handleResize)
  
  // 监听容器大小变化
  if (chartRef.value && window.ResizeObserver) {
    resizeObserver = new ResizeObserver(() => {
      handleResize()
    })
    resizeObserver.observe(chartRef.value)
  }
  
  // 延迟调用resize确保图表正确显示
  setTimeout(() => {
    handleResize()
  }, 100)
})

onUnmounted(() => {
  if (chartInstance) {
    chartInstance.dispose()
  }
  if (resizeObserver) {
    resizeObserver.disconnect()
  }
  window.removeEventListener('resize', handleResize)
})
</script>

<style scoped>
.training-chart {
  width: 100%;
  margin: 20px 0;
}

.chart-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.chart-header h4 {
  margin: 0;
  font-size: 16px;
  font-weight: 500;
}

.chart-container {
  width: 100%;
  min-width: 600px;
  height: v-bind(height);
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  box-sizing: border-box;
}
</style>