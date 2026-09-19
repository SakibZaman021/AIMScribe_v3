'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';

interface HealthScreening {
  bp_systolic: string;
  bp_diastolic: string;
  pulse_rate: string;
  diabetes_fasting: string;
  diabetes_random: string;
  height_cm: string;
  weight_kg: string;
  temperature: string;
}

interface PatientData {
  patient_id: string;
  patient_name: string;
  age: string;
  gender: string;
  doctor_id: string;
  hospital_id: string;
  health_screening: HealthScreening;
}

export default function EntryPage() {
  const router = useRouter();

  const [formData, setFormData] = useState<PatientData>({
    patient_id: '',
    patient_name: '',
    age: '',
    gender: '',
    doctor_id: 'DR001',
    hospital_id: 'HOSP001',
    health_screening: {
      bp_systolic: '',
      bp_diastolic: '',
      pulse_rate: '',
      diabetes_fasting: '',
      diabetes_random: '',
      height_cm: '',
      weight_kg: '',
      temperature: '',
    },
  });

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, value } = e.target;

    if (name.startsWith('hs_')) {
      const hsField = name.replace('hs_', '');
      setFormData(prev => ({
        ...prev,
        health_screening: {
          ...prev.health_screening,
          [hsField]: value,
        },
      }));
    } else {
      setFormData(prev => ({ ...prev, [name]: value }));
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();

    // Store in sessionStorage for dashboard
    sessionStorage.setItem('patientData', JSON.stringify(formData));

    // Navigate to dashboard
    router.push('/dashboard');
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100 py-8">
      <div className="max-w-4xl mx-auto px-4">
        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold text-gray-800">CMED</h1>
          <p className="text-gray-600 mt-2">Patient Registration</p>
        </div>

        {/* Form Card */}
        <div className="bg-white rounded-xl shadow-lg p-8">
          <form onSubmit={handleSubmit} className="space-y-8">

            {/* Patient Information */}
            <div>
              <h2 className="text-xl font-semibold text-gray-700 mb-4 pb-2 border-b">
                Patient Information
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Patient ID *
                  </label>
                  <input
                    type="text"
                    name="patient_id"
                    value={formData.patient_id}
                    onChange={handleChange}
                    required
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder="Enter Patient ID"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Patient Name *
                  </label>
                  <input
                    type="text"
                    name="patient_name"
                    value={formData.patient_name}
                    onChange={handleChange}
                    required
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder="Enter Patient Name"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Age
                  </label>
                  <input
                    type="text"
                    name="age"
                    value={formData.age}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder="e.g., 45"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Gender
                  </label>
                  <select
                    name="gender"
                    value={formData.gender}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                  >
                    <option value="">Select Gender</option>
                    <option value="Male">Male</option>
                    <option value="Female">Female</option>
                    <option value="Other">Other</option>
                  </select>
                </div>
              </div>
            </div>

            {/* Doctor & Hospital */}
            <div>
              <h2 className="text-xl font-semibold text-gray-700 mb-4 pb-2 border-b">
                Doctor & Hospital
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Doctor ID
                  </label>
                  <input
                    type="text"
                    name="doctor_id"
                    value={formData.doctor_id}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Hospital ID
                  </label>
                  <input
                    type="text"
                    name="hospital_id"
                    value={formData.hospital_id}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                  />
                </div>
              </div>
            </div>

            {/* Health Screening */}
            <div>
              <h2 className="text-xl font-semibold text-gray-700 mb-4 pb-2 border-b">
                Health Screening
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    BP Systolic (mmHg)
                  </label>
                  <input
                    type="text"
                    name="hs_bp_systolic"
                    value={formData.health_screening.bp_systolic}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder="120"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    BP Diastolic (mmHg)
                  </label>
                  <input
                    type="text"
                    name="hs_bp_diastolic"
                    value={formData.health_screening.bp_diastolic}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder="80"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Pulse Rate (bpm)
                  </label>
                  <input
                    type="text"
                    name="hs_pulse_rate"
                    value={formData.health_screening.pulse_rate}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder="72"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Temperature (°C)
                  </label>
                  <input
                    type="text"
                    name="hs_temperature"
                    value={formData.health_screening.temperature}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder="98.6"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Fasting Sugar (mg/dL)
                  </label>
                  <input
                    type="text"
                    name="hs_diabetes_fasting"
                    value={formData.health_screening.diabetes_fasting}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder=""
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Random Sugar (mg/dL)
                  </label>
                  <input
                    type="text"
                    name="hs_diabetes_random"
                    value={formData.health_screening.diabetes_random}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder=""
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Height (cm)
                  </label>
                  <input
                    type="text"
                    name="hs_height_cm"
                    value={formData.health_screening.height_cm}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder="170"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    Weight (kg)
                  </label>
                  <input
                    type="text"
                    name="hs_weight_kg"
                    value={formData.health_screening.weight_kg}
                    onChange={handleChange}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    placeholder="70"
                  />
                </div>
              </div>
            </div>

            {/* Submit Button */}
            <div className="flex justify-end pt-4">
              <button
                type="submit"
                className="px-8 py-3 bg-blue-600 text-white font-semibold rounded-lg hover:bg-blue-700 focus:ring-4 focus:ring-blue-300 transition-colors"
              >
                Go to Doctor Dashboard →
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
