// MD5: 16d716496bd59f0690fbad10a93a3a82
/*
 * 版权所有 ©  2025  羊羊小栈 (GJQ)
 *
 * 作者：羊羊小栈
 * 时间：2025-09-06 23:44:27
 *
 * 本系统为原创作品，禁止二次销售！禁止将系统对应的视频、文档进行二次发布！
 * 违者需立即停止侵权行为，并按照【羊羊小栈系统版权声明及保护条款】中规定数额进行赔偿，并承担相应法律责任。
 */

import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'

// 获取多折线图配置
export const getMultiLineChartOptions = (
  xData: string[],
  series: Array<{
    name: string
    data: number[]
    color: string
  }>
): EChartsOption => {
  return {
    tooltip: {
      trigger: 'axis'
    },
    legend: {
      data: series.map(item => item.name)
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: xData
    },
    yAxis: {
      type: 'value'
    },
    series: series.map(item => ({
      name: item.name,
      type: 'line',
      smooth: true,
      data: item.data,
      itemStyle: {
        color: item.color
      },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          {
            offset: 0,
            color: item.color.replace(')', ', 0.3)')
          },
          {
            offset: 1,
            color: item.color.replace(')', ', 0.1)')
          }
        ])
      }
    }))
  }
}

// 获取饼图配置
export const getPieChartOptions = (
  data: Array<{
    name: string
    value: number
    color?: string
  }>,
  title?: string
): EChartsOption => {
  return {
    title: title ? {
      text: title,
      left: 'center'
    } : undefined,
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c} ({d}%)'
    },
    legend: {
      orient: 'vertical',
      left: 'left'
    },
    series: [
      {
        type: 'pie',
        radius: '70%',
        data: data.map(item => ({
          ...item,
          itemStyle: item.color ? { color: item.color } : undefined
        })),
        emphasis: {
          itemStyle: {
            shadowBlur: 10,
            shadowOffsetX: 0,
            shadowColor: 'rgba(0, 0, 0, 0.5)'
          }
        }
      }
    ]
  }
}

// 获取柱状图配置
export const getBarChartOptions = (
  xData: string[],
  series: Array<{
    name: string
    data: number[]
    color?: string
  }>,
  title?: string
): EChartsOption => {
  return {
    title: title ? {
      text: title,
      left: 'center'
    } : undefined,
    tooltip: {
      trigger: 'axis'
    },
    legend: {
      data: series.map(item => item.name)
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      data: xData
    },
    yAxis: {
      type: 'value'
    },
    series: series.map(item => ({
      name: item.name,
      type: 'bar',
      data: item.data,
      itemStyle: item.color ? { color: item.color } : undefined
    }))
  }
}

// 获取环形图配置
export const getDoughnutChartOptions = (
  data: Array<{
    name: string
    value: number
    color?: string
  }>,
  title?: string
): EChartsOption => {
  return {
    title: title ? {
      text: title,
      left: 'center'
    } : undefined,
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c} ({d}%)'
    },
    legend: {
      orient: 'vertical',
      left: 'left'
    },
    series: [
      {
        type: 'pie',
        radius: ['50%', '70%'],
        avoidLabelOverlap: false,
        label: {
          show: false,
          position: 'center'
        },
        emphasis: {
          label: {
            show: true,
            fontSize: 16,
            fontWeight: 'bold'
          }
        },
        labelLine: {
          show: false
        },
        data: data.map(item => ({
          ...item,
          itemStyle: item.color ? { color: item.color } : undefined
        }))
      }
    ]
  }
} 